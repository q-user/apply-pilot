"""TDD tests for the ``apply_worker`` rate limiter (M5, issue #46).

The rate limiter lives in :mod:`job_apply.features.apply_worker.limits`
and gates :meth:`ApplyJobService.enqueue_for_match` on a per-user
hourly / daily cap. The slice is wired through DI: tests construct
:class:`InMemoryRateLimiter` directly, production wires the
:class:`SqlRateLimiter` in :mod:`api`.

Test surface
------------

The 10 test cases cover:

* the in-memory limiter under / at / over the limit;
* the daily and hourly windows are evaluated independently;
* ``record`` increments the counter for the matched window;
* user isolation — User A's limit does not bleed into User B;
* window expiry — the counter resets when the clock advances past
  the window boundary;
* the service raises :class:`RateLimitExceeded` when the user is
  over the limit;
* the HTTP ``GET /apply-jobs/limits`` endpoint returns the current
  used / remaining / reset snapshot.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from job_apply.config import ApplyWorkerSettings
from job_apply.features.apply_worker.limits import (
    InMemoryRateLimiter,
    RateLimiter,
    RateLimitExceeded,
    SqlRateLimiter,
)
from job_apply.features.apply_worker.repository import (
    InMemoryApplyJobRepository,
    InMemoryApplyStatusHistoryRepository,
)
from job_apply.features.apply_worker.service import ApplyJobService
from job_apply.features.matches.models import VacancyMatch
from job_apply.features.search_profiles.models import SearchProfile
from job_apply.features.users.security import issue_token

# ---------------------------------------------------------------------------
# Fakes / helpers
# ---------------------------------------------------------------------------


class _TickingClock:
    """A wall clock the test can advance manually.

    Returns ``datetime`` values that match :func:`datetime.now` (timezone-
    aware UTC) so the limiter does not have to special-case naive
    datetimes. Tests use :meth:`advance` to step the clock past a window
    boundary.
    """

    def __init__(self, start: datetime | None = None) -> None:
        self._now: datetime = start or datetime(2026, 6, 16, 12, 0, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self._now

    def advance(self, delta: timedelta) -> None:
        self._now = self._now + delta


def _settings(*, hourly_limit: int = 10, daily_limit: int = 30) -> ApplyWorkerSettings:
    return ApplyWorkerSettings(
        max_attempts=3,
        base_delay_seconds=2.0,
        max_delay_seconds=300.0,
        backoff_multiplier=2.0,
        jitter=True,
        hourly_limit=hourly_limit,
        daily_limit=daily_limit,
    )


@dataclass
class _FakeMatchRepo:
    matches: dict[uuid.UUID, VacancyMatch] = field(default_factory=dict)

    def get_by_id(self, match_id: uuid.UUID) -> VacancyMatch | None:
        return self.matches.get(match_id)

    def add(self, match: VacancyMatch) -> VacancyMatch:
        self.matches[match.id] = match
        return match


@dataclass
class _FakeProfileRepo:
    profiles: dict[uuid.UUID, SearchProfile] = field(default_factory=dict)

    def get_by_id(self, profile_id: uuid.UUID) -> SearchProfile | None:
        return self.profiles.get(profile_id)

    def add(self, profile: SearchProfile) -> SearchProfile:
        self.profiles[profile.id] = profile
        return profile


@dataclass
class _World:
    user_id: uuid.UUID
    other_user_id: uuid.UUID
    profile: SearchProfile
    other_profile: SearchProfile
    vacancy_id: uuid.UUID
    match: VacancyMatch
    other_match: VacancyMatch
    match_repo: _FakeMatchRepo
    profile_repo: _FakeProfileRepo
    job_repo: InMemoryApplyJobRepository
    history_repo: InMemoryApplyStatusHistoryRepository
    clock: _TickingClock
    settings: ApplyWorkerSettings
    rate_limiter: InMemoryRateLimiter
    service: ApplyJobService


def _make_world(
    *,
    hourly_limit: int = 10,
    daily_limit: int = 30,
) -> _World:
    user_id = uuid.uuid4()
    other_user_id = uuid.uuid4()
    profile = SearchProfile(
        id=uuid.uuid4(),
        user_id=user_id,
        title="Senior Python",
        keywords="python, fastapi",
        is_active=True,
    )
    other_profile = SearchProfile(
        id=uuid.uuid4(),
        user_id=other_user_id,
        title="Other",
        keywords="x",
        is_active=True,
    )
    vacancy_id = uuid.uuid4()
    match = VacancyMatch(
        id=uuid.uuid4(),
        search_profile_id=profile.id,
        vacancy_id=vacancy_id,
        status="accepted",
    )
    other_match = VacancyMatch(
        id=uuid.uuid4(),
        search_profile_id=other_profile.id,
        vacancy_id=uuid.uuid4(),
        status="accepted",
    )
    match_repo = _FakeMatchRepo()
    match_repo.add(match)
    match_repo.add(other_match)
    profile_repo = _FakeProfileRepo()
    profile_repo.add(profile)
    profile_repo.add(other_profile)
    job_repo = InMemoryApplyJobRepository()
    history_repo = InMemoryApplyStatusHistoryRepository()
    clock = _TickingClock()
    settings = _settings(hourly_limit=hourly_limit, daily_limit=daily_limit)
    rate_limiter = InMemoryRateLimiter(settings=settings, clock=clock)
    service = ApplyJobService(
        job_repo=job_repo,
        match_repo=match_repo,  # type: ignore[arg-type]
        profile_repo=profile_repo,  # type: ignore[arg-type]
        history_repo=history_repo,
        rate_limiter=rate_limiter,
    )
    return _World(
        user_id=user_id,
        other_user_id=other_user_id,
        profile=profile,
        other_profile=other_profile,
        vacancy_id=vacancy_id,
        match=match,
        other_match=other_match,
        match_repo=match_repo,
        profile_repo=profile_repo,
        job_repo=job_repo,
        history_repo=history_repo,
        clock=clock,
        settings=settings,
        rate_limiter=rate_limiter,
        service=service,
    )


# ---------------------------------------------------------------------------
# InMemoryRateLimiter — window math
# ---------------------------------------------------------------------------


def test_under_limit_allows(world: _World) -> None:
    """Below the limit the limiter reports ``allowed=True``."""
    for _ in range(5):
        world.rate_limiter.record(world.user_id, key="apply")
    result = world.rate_limiter.check(world.user_id, key="apply")
    assert result.allowed is True
    assert result.reason is None
    assert result.retry_after_seconds is None
    # The window snapshot still reports 5 used out of 10.
    assert result.hourly.used == 5
    assert result.hourly.limit == 10
    assert result.hourly.remaining == 5
    assert result.daily.used == 5
    assert result.daily.limit == 30
    assert result.daily.remaining == 25


def test_at_limit_blocks(world: _World) -> None:
    """Hitting the limit flips ``allowed`` to ``False`` with a clear reason."""
    for _ in range(10):
        world.rate_limiter.record(world.user_id, key="apply")

    result = world.rate_limiter.check(world.user_id, key="apply")

    assert result.allowed is False
    assert result.reason == "rate_limit_exceeded"
    assert result.retry_after_seconds is not None
    # The snapshot reflects the saturated window.
    assert result.hourly.used == 10
    assert result.hourly.remaining == 0


def test_over_limit_blocks_with_retry_after(world: _World) -> None:
    """``retry_after_seconds`` points at the next hourly boundary."""
    for _ in range(11):
        world.rate_limiter.record(world.user_id, key="apply")

    result = world.rate_limiter.check(world.user_id, key="apply")

    assert result.allowed is False
    assert result.reason == "rate_limit_exceeded"
    assert result.retry_after_seconds is not None
    # First record was at the test's start time; retry_after is the
    # seconds until that record falls out of the 1-hour window.
    assert 0 < result.retry_after_seconds <= 3600


def test_daily_limit_separate_from_hourly(world: _World) -> None:
    """The daily cap is enforced independently of the hourly cap.

    A user can be inside the hourly cap but still blocked by the
    daily cap when the total within 24 hours exceeds the daily limit.
    """
    daily_world = _make_world(hourly_limit=10, daily_limit=5)
    # 5 records puts the user at the daily cap and inside the hourly
    # cap (5 < 10). The next check must be blocked by the daily cap.
    for _ in range(5):
        daily_world.rate_limiter.record(daily_world.user_id, key="apply")

    result = daily_world.rate_limiter.check(daily_world.user_id, key="apply")

    assert result.allowed is False
    assert result.reason == "rate_limit_exceeded"
    # The daily snapshot reports the breach.
    assert result.daily.used == 5
    assert result.daily.limit == 5
    assert result.daily.remaining == 0
    # And the hourly window is still under its cap — it is the daily
    # window that drove the rejection.
    assert result.hourly.used == 5
    assert result.hourly.remaining == 5


def test_record_increments_counter(world: _World) -> None:
    """``record`` increments both the hourly and daily counters for the key."""
    first = world.rate_limiter.check(world.user_id, key="apply")
    assert first.hourly.used == 0
    assert first.daily.used == 0

    world.rate_limiter.record(world.user_id, key="apply")
    world.rate_limiter.record(world.user_id, key="apply")

    second = world.rate_limiter.check(world.user_id, key="apply")
    assert second.hourly.used == 2
    assert second.daily.used == 2
    assert second.hourly.remaining == 8
    assert second.daily.remaining == 28


def test_different_users_have_separate_limits(world: _World) -> None:
    """User A hitting the limit does not affect User B."""
    for _ in range(10):
        world.rate_limiter.record(world.user_id, key="apply")

    blocked = world.rate_limiter.check(world.user_id, key="apply")
    other = world.rate_limiter.check(world.other_user_id, key="apply")

    assert blocked.allowed is False
    assert other.allowed is True
    assert other.hourly.used == 0
    assert other.daily.used == 0


def test_limits_reset_after_window(world: _World) -> None:
    """The counter resets once the clock moves past the hourly window."""
    for _ in range(10):
        world.rate_limiter.record(world.user_id, key="apply")
    blocked = world.rate_limiter.check(world.user_id, key="apply")
    assert blocked.allowed is False

    # Step the clock past the 1-hour window boundary; the 10 records
    # fall out of the window and the user is allowed again.
    world.clock.advance(timedelta(hours=1, minutes=1))

    result = world.rate_limiter.check(world.user_id, key="apply")
    assert result.allowed is True
    assert result.hourly.used == 0
    assert result.daily.used == 10  # daily window is still 24h wide


# ---------------------------------------------------------------------------
# Service integration
# ---------------------------------------------------------------------------


def test_enqueue_for_match_respects_rate_limit(world: _World) -> None:
    """``enqueue_for_match`` raises once the user is over the hourly cap."""
    # Saturate the hourly window with records (one per enqueue call).
    for _ in range(10):
        world.rate_limiter.record(world.user_id, key="apply")

    with pytest.raises(RateLimitExceeded):
        world.service.enqueue_for_match(world.match.id)


def test_rate_limit_exceeded_raises(world: _World) -> None:
    """The exception carries the limiter's ``retry_after`` payload."""
    for _ in range(10):
        world.rate_limiter.record(world.user_id, key="apply")

    with pytest.raises(RateLimitExceeded) as excinfo:
        world.service.enqueue_for_match(world.match.id)

    # The exception's ``result`` attribute exposes the structured payload
    # the HTTP layer uses to build a 429 response.
    assert excinfo.value.retry_after_seconds is not None
    assert 0 < excinfo.value.retry_after_seconds <= 3600
    assert excinfo.value.reason == "rate_limit_exceeded"


# ---------------------------------------------------------------------------
# HTTP — GET /apply-jobs/limits
# ---------------------------------------------------------------------------


def test_api_limits_endpoint() -> None:
    """``GET /apply-jobs/limits`` returns the current rate-limit snapshot.

    The endpoint does not require a record to have been made; the
    snapshot reflects "no submissions yet" with the full quota intact.
    """
    world = _make_world(hourly_limit=10, daily_limit=30)
    # Record two events so the snapshot is non-trivial.
    world.rate_limiter.record(world.user_id, key="apply")

    application = FastAPI()
    from job_apply.features.apply_worker.api import (
        get_apply_job_service,
    )
    from job_apply.features.apply_worker.api import (
        router as apply_worker_router,
    )

    application.include_router(apply_worker_router)
    application.dependency_overrides[get_apply_job_service] = lambda: world.service

    token = issue_token(str(world.user_id), ttl_seconds=3600)
    with TestClient(application) as client:
        response = client.get("/apply-jobs/limits", headers={"Authorization": f"Bearer {token}"})

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["hourly"] == {
        "used": 1,
        "limit": 10,
        "remaining": 9,
        "reset_at": payload["hourly"]["reset_at"],
    }
    assert payload["daily"] == {
        "used": 1,
        "limit": 30,
        "remaining": 29,
        "reset_at": payload["daily"]["reset_at"],
    }
    # ``reset_at`` is an ISO-8601 timestamp; it round-trips through
    # ``datetime.fromisoformat`` so the dashboard can render it.
    assert datetime.fromisoformat(payload["hourly"]["reset_at"]) is not None
    assert datetime.fromisoformat(payload["daily"]["reset_at"]) is not None


# ---------------------------------------------------------------------------
# Sanity checks for the SQL implementation
# ---------------------------------------------------------------------------


def test_sql_rate_limiter_round_trip() -> None:
    """The SQL limiter stores events in the database and counts them.

    The test exercises the production ``SqlRateLimiter`` against an
    in-memory SQLite database so the SQL path is part of the CI
    surface.
    """
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from job_apply.db import Base
    from job_apply.features.apply_worker.models import ApplyRateLimitEvent

    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, autoflush=False)
    clock = _TickingClock()
    settings = _settings(hourly_limit=3, daily_limit=3)
    limiter: RateLimiter = SqlRateLimiter(
        session_factory=session_factory,
        settings=settings,
        clock=clock,
    )

    user_id = uuid.uuid4()
    for _ in range(3):
        limiter.record(user_id, key="apply")
    blocked = limiter.check(user_id, key="apply")
    assert blocked.allowed is False
    assert blocked.reason == "rate_limit_exceeded"
    # The events actually landed in the table.
    with session_factory() as session:
        rows = session.query(ApplyRateLimitEvent).all()
        assert len(rows) == 3


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def world() -> _World:
    return _make_world()


# Exported for tests that build their own ``_World`` instance.
__all__ = ["_TickingClock", "_World", "_make_world", "_settings"]
