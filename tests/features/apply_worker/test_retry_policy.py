"""TDD tests for the apply-worker retry policy (M5, issue #47).

The :class:`RetryPolicy` owns the "when do we requeue a failed apply
job, and when do we dead-letter it?" decision. The tests in this module
exercise the policy in isolation (math) and in integration with the
:class:`ApplyJobService` (dead-letter transition).

Tests rely only on the public dataclass API and inject a seeded
``random.Random`` instance so the jitter behaviour is deterministic.

Coverage:

* exponential backoff with no jitter
* jittered backoff within the ±10% band
* the delay cap is honoured past ``max_delay_seconds``
* ``should_retry`` boundary at ``max_attempts``
* :class:`ApplyJobService` uses the policy to compute ``next_run_at``
  and dead-letters once ``max_attempts`` is exceeded
* ``ApplyWorkerSettings`` is loaded from environment variables
"""

from __future__ import annotations

import random
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

import pytest

from job_apply.config import (
    ApplyWorkerSettings,
    get_apply_worker_settings,
)
from job_apply.features.apply_worker.models import ApplyJobStatus
from job_apply.features.apply_worker.repository import (
    InMemoryApplyJobRepository,
    InMemoryApplyStatusHistoryRepository,
)
from job_apply.features.apply_worker.retry import RetryPolicy
from job_apply.features.apply_worker.service import ApplyJobService
from job_apply.features.matches.models import VacancyMatch
from job_apply.features.search_profiles.models import SearchProfile

# Local alias to avoid a top-level import we have not pinned down.
MatchStatus = __import__("job_apply.features.matches.models", fromlist=["MatchStatus"]).MatchStatus


# ---------------------------------------------------------------------------
# RetryPolicy math
# ---------------------------------------------------------------------------


def test_compute_next_run_at_with_no_jitter() -> None:
    """No jitter: ``attempts=1`` → ``base_delay``, then exponential growth."""
    policy = RetryPolicy(
        max_attempts=5,
        base_delay_seconds=2.0,
        max_delay_seconds=300.0,
        backoff_multiplier=2.0,
        jitter=False,
    )
    now = datetime(2026, 6, 16, 12, 0, 0, tzinfo=UTC)

    # attempts=1 → 2 * 2^0 = 2
    assert policy.compute_next_run_at(1, now=now) == now + timedelta(seconds=2)
    # attempts=2 → 2 * 2^1 = 4
    assert policy.compute_next_run_at(2, now=now) == now + timedelta(seconds=4)
    # attempts=3 → 2 * 2^2 = 8
    assert policy.compute_next_run_at(3, now=now) == now + timedelta(seconds=8)


def test_compute_next_run_at_with_jitter_in_range() -> None:
    """Jitter keeps the delay within ±10% of the unjittered value."""
    policy = RetryPolicy(
        max_attempts=5,
        base_delay_seconds=10.0,
        max_delay_seconds=300.0,
        backoff_multiplier=2.0,
        jitter=True,
    )
    rng = random.Random(0)
    now = datetime(2026, 6, 16, 12, 0, 0, tzinfo=UTC)
    unjittered = 10.0  # attempts=1 → 10.0
    low = unjittered * 0.9
    high = unjittered * 1.1

    for _ in range(200):
        result = policy.compute_next_run_at(1, now=now, rng=rng)
        delay = (result - now).total_seconds()
        assert low <= delay <= high, f"delay {delay} outside [{low}, {high}]"


def test_compute_next_run_at_caps_at_max_delay() -> None:
    """Backoff never exceeds ``max_delay_seconds``."""
    policy = RetryPolicy(
        max_attempts=20,
        base_delay_seconds=2.0,
        max_delay_seconds=300.0,
        backoff_multiplier=2.0,
        jitter=False,
    )
    now = datetime(2026, 6, 16, 12, 0, 0, tzinfo=UTC)

    # attempts=10 → 2 * 2^9 = 1024, capped to 300
    assert policy.compute_next_run_at(10, now=now) == now + timedelta(seconds=300)
    assert policy.compute_next_run_at(20, now=now) == now + timedelta(seconds=300)


def test_compute_next_run_at_jitter_around_capped_value() -> None:
    """Jitter perturbs the capped delay within ±10% of ``max_delay_seconds``."""
    policy = RetryPolicy(
        max_attempts=20,
        base_delay_seconds=2.0,
        max_delay_seconds=300.0,
        backoff_multiplier=2.0,
        jitter=True,
    )
    rng = random.Random(42)
    now = datetime(2026, 6, 16, 12, 0, 0, tzinfo=UTC)
    low = 300.0 * 0.9
    high = 300.0 * 1.1

    for _ in range(100):
        result = policy.compute_next_run_at(15, now=now, rng=rng)
        delay = (result - now).total_seconds()
        assert low <= delay <= high, f"delay {delay} outside [{low}, {high}]"


def test_should_retry_returns_true_below_max_attempts() -> None:
    """``should_retry`` is True for any attempt strictly below ``max_attempts``."""
    policy = RetryPolicy(max_attempts=3)

    assert policy.should_retry(0) is True
    assert policy.should_retry(1) is True
    assert policy.should_retry(2) is True


def test_should_retry_returns_false_at_max_attempts() -> None:
    """``should_retry`` is False at or above ``max_attempts``."""
    policy = RetryPolicy(max_attempts=3)

    assert policy.should_retry(3) is False
    assert policy.should_retry(4) is False
    assert policy.should_retry(99) is False


def test_retry_policy_is_frozen() -> None:
    """``RetryPolicy`` is a frozen dataclass — assignment raises."""
    policy = RetryPolicy()
    with pytest.raises((AttributeError, TypeError)):
        policy.max_attempts = 10  # type: ignore[misc]


# ---------------------------------------------------------------------------
# ApplyJobService integration
# ---------------------------------------------------------------------------


@dataclass
class _FakeMatchRepo:
    """In-memory match repository exposing only ``get_by_id``."""

    matches: dict[uuid.UUID, VacancyMatch] = field(default_factory=dict)

    def get_by_id(self, match_id: uuid.UUID) -> VacancyMatch | None:
        return self.matches.get(match_id)

    def add(self, match: VacancyMatch) -> VacancyMatch:
        self.matches[match.id] = match
        return match


@dataclass
class _FakeProfileRepo:
    """In-memory search-profile repository exposing only ``get_by_id``."""

    profiles: dict[uuid.UUID, SearchProfile] = field(default_factory=dict)

    def get_by_id(self, profile_id: uuid.UUID) -> SearchProfile | None:
        return self.profiles.get(profile_id)

    def add(self, profile: SearchProfile) -> SearchProfile:
        self.profiles[profile.id] = profile
        return profile


@dataclass
class _World:
    """Tiny in-memory world wired up for one test."""

    user_id: uuid.UUID
    profile: SearchProfile
    vacancy_id: uuid.UUID
    match: VacancyMatch
    match_repo: _FakeMatchRepo
    profile_repo: _FakeProfileRepo
    job_repo: InMemoryApplyJobRepository
    service: ApplyJobService
    policy: RetryPolicy


def _make_world(*, policy: RetryPolicy | None = None) -> _World:
    user_id = uuid.uuid4()
    profile = SearchProfile(
        id=uuid.uuid4(),
        user_id=user_id,
        title="Senior Python",
        keywords="python, fastapi",
        is_active=True,
    )
    vacancy_id = uuid.uuid4()
    match = VacancyMatch(
        id=uuid.uuid4(),
        search_profile_id=profile.id,
        vacancy_id=vacancy_id,
        status=MatchStatus.ACCEPTED.value,
    )

    match_repo = _FakeMatchRepo()
    match_repo.add(match)
    profile_repo = _FakeProfileRepo()
    profile_repo.add(profile)

    job_repo = InMemoryApplyJobRepository()
    active_policy = policy or RetryPolicy()
    service = ApplyJobService(
        job_repo=job_repo,
        match_repo=match_repo,  # type: ignore[arg-type]
        profile_repo=profile_repo,  # type: ignore[arg-type]
        history_repo=InMemoryApplyStatusHistoryRepository(),
        retry_policy=active_policy,
    )
    return _World(
        user_id=user_id,
        profile=profile,
        vacancy_id=vacancy_id,
        match=match,
        match_repo=match_repo,
        profile_repo=profile_repo,
        job_repo=job_repo,
        service=service,
        policy=active_policy,
    )


def test_apply_job_service_uses_retry_policy() -> None:
    """``fail(retryable=True)`` schedules ``next_run_at`` via the injected policy."""
    policy = RetryPolicy(
        max_attempts=3,
        base_delay_seconds=2.0,
        max_delay_seconds=300.0,
        backoff_multiplier=2.0,
        jitter=False,
    )
    world = _make_world(policy=policy)
    job = world.service.enqueue_for_match(world.match.id)
    world.service.claim_next()

    before = datetime.now(UTC)
    failed = world.service.fail(job.id, error="transient", retryable=True)
    after = datetime.now(UTC)

    assert failed.status == ApplyJobStatus.QUEUED.value
    # claim_next → 1, mark_attempt → 2; compute_next_run_at(2) = 2 * 2^1 = 4s
    assert failed.attempts == 2
    assert failed.next_run_at is not None
    expected_min = before + timedelta(seconds=4)
    expected_max = after + timedelta(seconds=4)
    assert expected_min <= failed.next_run_at <= expected_max


def test_apply_job_service_marks_dead_letter_when_max_attempts_exceeded() -> None:
    """A retryable failure past ``max_attempts`` lands in ``dead_letter``.

    With ``max_attempts=1`` the policy refuses to requeue as soon as
    ``attempts >= 1`` (post-``mark_attempt``), so the first
    ``fail(retryable=True)`` is enough to drive the row to
    ``dead_letter`` without us having to wait for a backoff window
    before re-claiming.
    """
    policy = RetryPolicy(
        max_attempts=1,
        base_delay_seconds=2.0,
        max_delay_seconds=300.0,
        backoff_multiplier=2.0,
        jitter=False,
    )
    world = _make_world(policy=policy)
    job = world.service.enqueue_for_match(world.match.id)
    world.service.claim_next()  # attempts: 0 -> 1

    final = world.service.fail(job.id, error="boom", retryable=True)
    # ``mark_attempt`` bumps attempts: 1 -> 2. ``should_retry(2)`` is
    # ``False`` (2 >= max_attempts=1), so the policy refuses to requeue.
    assert final.status == ApplyJobStatus.DEAD_LETTER.value
    assert final.attempts == 2
    assert final.finished_at is not None
    # ``next_run_at`` is no longer relevant on a dead-lettered job.
    assert final.next_run_at is None


def test_apply_job_service_non_retryable_still_dead_letters() -> None:
    """``retryable=False`` keeps short-circuiting to ``dead_letter``."""
    world = _make_world()
    job = world.service.enqueue_for_match(world.match.id)
    world.service.claim_next()

    failed = world.service.fail(job.id, error="permanent", retryable=False)

    assert failed.status == ApplyJobStatus.DEAD_LETTER.value
    assert failed.last_error == "permanent"


# ---------------------------------------------------------------------------
# ApplyWorkerSettings (config)
# ---------------------------------------------------------------------------


def test_apply_worker_settings_defaults() -> None:
    """The dataclass has the documented sensible defaults."""
    settings = ApplyWorkerSettings()
    assert settings.max_attempts == 3
    assert settings.base_delay_seconds == 2.0
    assert settings.max_delay_seconds == 300.0
    assert settings.backoff_multiplier == 2.0
    assert settings.jitter is True


def test_apply_worker_settings_rejects_invalid_values() -> None:
    """The dataclass validates values eagerly in ``__post_init__``."""
    with pytest.raises(ValueError, match="max_attempts"):
        ApplyWorkerSettings(max_attempts=0)
    with pytest.raises(ValueError, match="base_delay_seconds"):
        ApplyWorkerSettings(base_delay_seconds=0)
    with pytest.raises(ValueError, match="max_delay_seconds"):
        ApplyWorkerSettings(max_delay_seconds=0)
    with pytest.raises(ValueError, match="backoff_multiplier"):
        ApplyWorkerSettings(backoff_multiplier=0)


def test_config_loads_apply_worker_settings_from_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``get_apply_worker_settings()`` reads ``APP_APPLY_*`` env vars."""
    monkeypatch.setenv("APP_APPLY_MAX_ATTEMPTS", "5")
    monkeypatch.setenv("APP_APPLY_BASE_DELAY_SECONDS", "4.0")
    monkeypatch.setenv("APP_APPLY_MAX_DELAY_SECONDS", "60.0")
    monkeypatch.setenv("APP_APPLY_BACKOFF_MULTIPLIER", "3.0")
    monkeypatch.setenv("APP_APPLY_JITTER", "false")

    settings = get_apply_worker_settings()

    assert settings.max_attempts == 5
    assert settings.base_delay_seconds == 4.0
    assert settings.max_delay_seconds == 60.0
    assert settings.backoff_multiplier == 3.0
    assert settings.jitter is False


def test_config_apply_worker_settings_defaults_when_env_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No env vars set → the documented defaults are returned."""
    for var in (
        "APP_APPLY_MAX_ATTEMPTS",
        "APP_APPLY_BASE_DELAY_SECONDS",
        "APP_APPLY_MAX_DELAY_SECONDS",
        "APP_APPLY_BACKOFF_MULTIPLIER",
        "APP_APPLY_JITTER",
    ):
        monkeypatch.delenv(var, raising=False)

    settings = get_apply_worker_settings()

    assert settings.max_attempts == 3
    assert settings.base_delay_seconds == 2.0
    assert settings.max_delay_seconds == 300.0
    assert settings.backoff_multiplier == 2.0
    assert settings.jitter is True


def test_config_rejects_invalid_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """A non-integer ``APP_APPLY_MAX_ATTEMPTS`` raises ``ValueError`` at load time."""
    monkeypatch.setenv("APP_APPLY_MAX_ATTEMPTS", "not-an-int")
    with pytest.raises(ValueError, match="APP_APPLY_MAX_ATTEMPTS"):
        get_apply_worker_settings()


def test_apply_worker_settings_to_retry_policy() -> None:
    """The settings object can build a :class:`RetryPolicy` with the same knobs."""
    settings = ApplyWorkerSettings(
        max_attempts=4,
        base_delay_seconds=1.5,
        max_delay_seconds=120.0,
        backoff_multiplier=1.5,
        jitter=False,
    )
    policy = settings.to_retry_policy()
    assert policy.max_attempts == 4
    assert policy.base_delay_seconds == 1.5
    assert policy.max_delay_seconds == 120.0
    assert policy.backoff_multiplier == 1.5
    assert policy.jitter is False
