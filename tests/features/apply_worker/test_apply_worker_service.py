"""TDD tests for the ``apply_worker`` service (M5, issue #43).

The :class:`ApplyJobService` is the integration seam between the
:class:`ApplyJobRepository` and the cross-slice lookups the queue needs
(``vacancy_matches`` and ``users``). Tests build a tiny in-memory world
with collaborator-injected fakes; production wiring in :mod:`api`
builds the service with the SQLAlchemy-backed repositories.

Test surface
------------

The 11 test cases cover:

* :meth:`enqueue_for_match` is idempotent (a second call returns the
  same row, no second insert) and the job carries the right
  ``user_id`` / ``vacancy_id`` / ``idempotency_key`` triple.
* :meth:`enqueue_for_match` refuses a missing match with a domain
  error.
* :meth:`list_user_jobs` returns the caller's jobs in newest-first
  order.
* :meth:`get` enforces ownership.
* :meth:`cancel` transitions a queued job to ``cancelled`` and refuses
  to cancel a terminal-state job (succeeded / dead_letter / cancelled).
* :meth:`claim_next` picks the next claimable row, transitions it to
  ``running``, and stamps ``started_at``.
* :meth:`complete` transitions to ``succeeded`` and stores the
  ``external_application_id``.
* :meth:`fail(retryable=True)` parks the job back in ``queued`` with a
  future ``next_run_at`` and records the error.
* :meth:`fail(retryable=False)` transitions to ``dead_letter`` and
  records the error.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field

import pytest

from job_apply.features.apply_worker.models import (
    ApplyJobStatus,
    compute_idempotency_key,
)
from job_apply.features.apply_worker.repository import InMemoryApplyJobRepository
from job_apply.features.apply_worker.service import (
    ApplyJobAlreadyTerminalError,
    ApplyJobDependencyMissingError,
    ApplyJobNotFoundError,
    ApplyJobOwnershipError,
    ApplyJobService,
)
from job_apply.features.matches.models import VacancyMatch
from job_apply.features.search_profiles.models import SearchProfile

# ---------------------------------------------------------------------------
# Fakes
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


def _make_world() -> _World:
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
    service = ApplyJobService(
        job_repo=job_repo,
        match_repo=match_repo,  # type: ignore[arg-type]
        profile_repo=profile_repo,  # type: ignore[arg-type]
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
    )


# Local alias to avoid a top-level import we have not pinned down.
MatchStatus = __import__("job_apply.features.matches.models", fromlist=["MatchStatus"]).MatchStatus


# ---------------------------------------------------------------------------
# enqueue_for_match
# ---------------------------------------------------------------------------


def test_enqueue_for_match_creates_a_queued_job(world: _World) -> None:
    """Enqueueing for a never-seen match creates a fresh ``queued`` row."""
    job = world.service.enqueue_for_match(world.match.id)

    assert job.id is not None
    assert job.match_id == world.match.id
    assert job.user_id == world.user_id
    assert job.vacancy_id == world.vacancy_id
    assert job.status == ApplyJobStatus.QUEUED.value
    assert job.attempts == 0
    assert job.idempotency_key == compute_idempotency_key(
        world.user_id, world.vacancy_id, world.match.id
    )
    assert world.job_repo.get_by_match(world.match.id) is job


def test_enqueue_for_match_is_idempotent(world: _World) -> None:
    """A second enqueue for the same match returns the existing row."""
    first = world.service.enqueue_for_match(world.match.id)
    second = world.service.enqueue_for_match(world.match.id)

    assert first.id == second.id
    # The repo must hold exactly one row for the match.
    listed = list(world.job_repo.list_by_user(world.user_id))
    assert len(listed) == 1
    assert listed[0].id == first.id


def test_enqueue_for_match_raises_when_match_missing(world: _World) -> None:
    """A missing match is a domain error, not a silent insert with null FKs."""
    with pytest.raises(ApplyJobDependencyMissingError):
        world.service.enqueue_for_match(uuid.uuid4())


def test_enqueue_for_match_raises_when_profile_missing(world: _World) -> None:
    """A missing search profile (orphaned match) is a domain error too."""
    # Build a match that points at a profile id that does not exist.
    orphan_match = VacancyMatch(
        id=uuid.uuid4(),
        search_profile_id=uuid.uuid4(),
        vacancy_id=uuid.uuid4(),
        status=MatchStatus.ACCEPTED.value,
    )
    world.match_repo.add(orphan_match)

    with pytest.raises(ApplyJobDependencyMissingError):
        world.service.enqueue_for_match(orphan_match.id)


# ---------------------------------------------------------------------------
# list_user_jobs / get
# ---------------------------------------------------------------------------


def test_list_user_jobs_returns_user_rows_in_newest_first_order(
    world: _World,
) -> None:
    """The dashboard listing shows the caller's jobs newest first."""
    # Two jobs on the same user via two different matches.
    second_match = VacancyMatch(
        id=uuid.uuid4(),
        search_profile_id=world.profile.id,
        vacancy_id=uuid.uuid4(),
        status=MatchStatus.ACCEPTED.value,
    )
    world.match_repo.add(second_match)

    first = world.service.enqueue_for_match(world.match.id)
    second = world.service.enqueue_for_match(second_match.id)

    listed = world.service.list_user_jobs(world.user_id)

    assert [j.id for j in listed] == [second.id, first.id]


def test_get_returns_row_with_ownership(world: _World) -> None:
    """``get`` returns the row when the caller owns it."""
    job = world.service.enqueue_for_match(world.match.id)

    fetched = world.service.get(job.id, user_id=world.user_id)

    assert fetched.id == job.id
    assert fetched.match_id == world.match.id


def test_get_raises_not_found_for_unknown_job(world: _World) -> None:
    """Missing job → :class:`ApplyJobNotFoundError`."""
    with pytest.raises(ApplyJobNotFoundError):
        world.service.get(uuid.uuid4(), user_id=world.user_id)


def test_get_raises_ownership_for_other_user(world: _World) -> None:
    """A different user sees a 403-mapped error."""
    job = world.service.enqueue_for_match(world.match.id)

    with pytest.raises(ApplyJobOwnershipError):
        world.service.get(job.id, user_id=uuid.uuid4())


# ---------------------------------------------------------------------------
# cancel
# ---------------------------------------------------------------------------


def test_cancel_transitions_queued_job(world: _World) -> None:
    """Cancelling a queued job flips it to ``cancelled``."""
    job = world.service.enqueue_for_match(world.match.id)

    cancelled = world.service.cancel(job.id, user_id=world.user_id)

    assert cancelled.status == ApplyJobStatus.CANCELLED.value
    assert cancelled.finished_at is not None


def test_cancel_refuses_terminal_job(world: _World) -> None:
    """A succeeded / dead_letter / cancelled job cannot be cancelled twice."""
    job = world.service.enqueue_for_match(world.match.id)
    # Walk the row to ``succeeded`` to simulate a completed run.
    world.service.claim_next()
    world.service.complete(job.id, external_application_id="hh-1")

    with pytest.raises(ApplyJobAlreadyTerminalError):
        world.service.cancel(job.id, user_id=world.user_id)


# ---------------------------------------------------------------------------
# claim_next / complete / fail
# ---------------------------------------------------------------------------


def test_claim_next_returns_queued_job_and_marks_running(world: _World) -> None:
    """``claim_next`` returns the oldest job and transitions it to running."""
    world.service.enqueue_for_match(world.match.id)

    claimed = world.service.claim_next()

    assert claimed is not None
    assert claimed.match_id == world.match.id
    assert claimed.status == ApplyJobStatus.RUNNING.value
    assert claimed.started_at is not None


def test_claim_next_returns_none_when_empty(world: _World) -> None:
    """``claim_next`` is a no-op when the queue is empty."""
    assert world.service.claim_next() is None


def test_complete_records_external_application_id(world: _World) -> None:
    """``complete`` transitions to ``succeeded`` and stores hh's app id."""
    job = world.service.enqueue_for_match(world.match.id)
    world.service.claim_next()

    completed = world.service.complete(job.id, external_application_id="hh-app-99")

    assert completed.status == ApplyJobStatus.SUCCEEDED.value
    assert completed.external_application_id == "hh-app-99"
    assert completed.finished_at is not None
    assert completed.last_error is None


def test_fail_retryable_park_job_with_backoff(world: _World) -> None:
    """``fail(retryable=True)`` puts the job back in ``queued`` with a future timestamp."""
    job = world.service.enqueue_for_match(world.match.id)
    world.service.claim_next()

    failed = world.service.fail(job.id, error="transient", retryable=True)

    assert failed.status == ApplyJobStatus.QUEUED.value
    assert failed.last_error == "transient"
    # ``claim_next`` bumped ``attempts`` to 1; ``mark_attempt`` (called
    # from ``fail``) bumps it to 2.
    assert failed.attempts == 2
    assert failed.next_run_at is not None
    # The retryable job is no longer claimable until ``next_run_at``
    # falls into the past.
    assert world.service.claim_next() is None


def test_fail_non_retryable_park_to_dead_letter(world: _World) -> None:
    """``fail(retryable=False)`` parks the job in ``dead_letter``."""
    job = world.service.enqueue_for_match(world.match.id)
    world.service.claim_next()

    failed = world.service.fail(job.id, error="permanent", retryable=False)

    assert failed.status == ApplyJobStatus.DEAD_LETTER.value
    assert failed.last_error == "permanent"
    assert failed.finished_at is not None
    # A dead-lettered job is terminal: ``claim_next`` skips it.
    assert world.service.claim_next() is None


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def world() -> _World:
    return _make_world()
