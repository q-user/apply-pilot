"""TDD tests for the apply worker runtime (M5, issue #44).

The :class:`ApplyWorker` is the per-iteration loop body: claim one job,
pick the right adapter, dispatch, and walk the lifecycle (``succeeded``
/ requeued / ``dead_letter``). The :class:`ApplyWorkerProcess` wraps
that loop in a :class:`BaseProcess` so the OS signal handlers are
installed and ``asyncio.sleep`` is interleaved with work.

Test surface
------------

* :meth:`process_one` returns ``None`` when the queue is empty.
* :meth:`process_one` dispatches to the adapter keyed by the vacancy's
  ``source`` field.
* :meth:`process_one` records a successful submission as
  ``succeeded`` and flips the underlying match to ``applied``.
* :meth:`process_one` parks a ``retryable=True`` failure back in
  ``queued`` while ``attempts < max_attempts`` and uses an
  exponential ``next_run_at`` delay.
* :meth:`process_one` walks a retryable failure that exhausted the
  budget to ``dead_letter``.
* :meth:`process_one` walks a ``retryable=False`` failure straight to
  ``dead_letter``.
* :meth:`process_one` walks a job whose vacancy has no registered
  adapter to ``dead_letter`` with the ``no_adapter_for_source`` error.
* :meth:`ApplyWorkerProcess.run` keeps calling :meth:`process_one` and
  honours the configured :attr:`poll_interval_seconds` between
  iterations.
* :meth:`ApplyWorkerProcess.run` exits as soon as a shutdown signal
  is observed (``BaseProcess`` contract).
"""

from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime

import pytest

from job_apply.features.apply_worker.models import ApplyJobStatus
from job_apply.features.apply_worker.repository import InMemoryApplyJobRepository
from job_apply.features.apply_worker.runtime import (
    DEFAULT_MAX_ATTEMPTS,
    ApplyResult,
    ApplyWorker,
    ApplyWorkerProcess,
)
from job_apply.features.apply_worker.service import ApplyJobService
from job_apply.features.matches.models import MatchStatus, VacancyMatch
from job_apply.features.matches.repository import InMemoryVacancyMatchRepository
from job_apply.features.matches.service import MatchService
from job_apply.features.search_profiles.models import SearchProfile
from job_apply.features.sources.models import Vacancy

# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


@dataclass
class _FakeProfileRepo:
    """Profile repository stub exposing only :meth:`get_by_id`."""

    profiles: dict[uuid.UUID, SearchProfile] = field(default_factory=dict)

    def get_by_id(self, profile_id: uuid.UUID) -> SearchProfile | None:
        return self.profiles.get(profile_id)

    def add(self, profile: SearchProfile) -> SearchProfile:
        self.profiles[profile.id] = profile
        return profile


@dataclass
class _FakeVacancyRepo:
    """Vacancy repository stub exposing only :meth:`get_by_id`."""

    vacancies: dict[uuid.UUID, Vacancy] = field(default_factory=dict)

    def get_by_id(self, vacancy_id: uuid.UUID) -> Vacancy | None:
        return self.vacancies.get(vacancy_id)

    def add(self, vacancy: Vacancy) -> Vacancy:
        self.vacancies[vacancy.id] = vacancy
        return vacancy


class _FakeAdapter:
    """Recording adapter that returns a configurable :class:`ApplyResult`."""

    def __init__(self, name: str, result: ApplyResult) -> None:
        self.name = name
        self._result = result
        self.submitted: list[uuid.UUID] = []

    async def submit(self, job: object) -> ApplyResult:
        # ``job`` is an :class:`ApplyJob`; we record its id for assertions.
        self.submitted.append(job.id)  # type: ignore[attr-defined]
        return self._result

    @property
    def call_count(self) -> int:
        return len(self.submitted)


# ---------------------------------------------------------------------------
# World builder
# ---------------------------------------------------------------------------


@dataclass
class _World:
    user_id: uuid.UUID
    profile: SearchProfile
    vacancy: Vacancy
    match: VacancyMatch
    job_repo: InMemoryApplyJobRepository
    profile_repo: _FakeProfileRepo
    vacancy_repo: _FakeVacancyRepo
    job_service: ApplyJobService
    match_service: MatchService
    adapters: dict[str, _FakeAdapter]


def _make_world(
    *,
    source: str = "hh",
    adapters: dict[str, _FakeAdapter] | None = None,
    job_result: ApplyResult | None = None,
) -> _World:
    user_id = uuid.uuid4()
    profile = SearchProfile(
        id=uuid.uuid4(),
        user_id=user_id,
        title="Senior Python",
        keywords="python, fastapi",
        is_active=True,
    )
    vacancy = Vacancy(
        id=uuid.uuid4(),
        source=source,
        source_id=f"{source}-12345",
        title="Senior Python Developer",
        raw_data={"title": "Senior Python Developer"},
    )
    match = VacancyMatch(
        id=uuid.uuid4(),
        search_profile_id=profile.id,
        vacancy_id=vacancy.id,
        status=MatchStatus.ACCEPTED.value,
    )

    profile_repo = _FakeProfileRepo()
    profile_repo.add(profile)
    vacancy_repo = _FakeVacancyRepo()
    vacancy_repo.add(vacancy)

    # The ``ApplyJobService`` only needs ``get_by_id`` on the match repo;
    # we point it at the same in-memory repo used by ``MatchService`` so
    # the two views of the match stay consistent.
    match_repo = InMemoryVacancyMatchRepository()
    match_repo.create(match)
    job_repo = InMemoryApplyJobRepository()
    job_service = ApplyJobService(
        job_repo=job_repo,
        match_repo=match_repo,  # type: ignore[arg-type]
        profile_repo=profile_repo,  # type: ignore[arg-type]
    )
    match_service = MatchService(
        match_repo=match_repo,  # type: ignore[arg-type]
        profile_repo=profile_repo,  # type: ignore[arg-type]
    )

    if adapters is None:
        if job_result is None:
            job_result = ApplyResult(
                success=True,
                external_application_id="hh-app-1",
                error=None,
                retryable=False,
            )
        adapters = {"hh": _FakeAdapter("hh", job_result)}

    return _World(
        user_id=user_id,
        profile=profile,
        vacancy=vacancy,
        match=match,
        job_repo=job_repo,
        profile_repo=profile_repo,
        vacancy_repo=vacancy_repo,
        job_service=job_service,
        match_service=match_service,
        adapters=adapters,
    )


def _make_worker(
    world: _World,
    *,
    vacancy_repo: _FakeVacancyRepo | None = None,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
) -> ApplyWorker:
    return ApplyWorker(
        job_service=world.job_service,
        match_service=world.match_service,
        vacancy_repo=vacancy_repo if vacancy_repo is not None else world.vacancy_repo,
        adapters=world.adapters,
        max_attempts=max_attempts,
    )


# ---------------------------------------------------------------------------
# process_one — no pending jobs
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_process_one_with_no_pending_returns_none() -> None:
    """An empty queue yields ``None`` and does not touch any adapter."""
    world = _make_world()
    worker = _make_worker(world)

    result = await worker.process_one()

    assert result is None
    for adapter in world.adapters.values():
        assert adapter.call_count == 0


# ---------------------------------------------------------------------------
# process_one — adapter dispatch
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_process_one_calls_correct_adapter_for_source() -> None:
    """The worker dispatches to the adapter keyed by ``vacancy.source``."""
    adapters = {
        "hh": _FakeAdapter(
            "hh",
            ApplyResult(
                success=True,
                external_application_id="hh-1",
                error=None,
                retryable=False,
            ),
        ),
        "habr": _FakeAdapter(
            "habr",
            ApplyResult(
                success=True,
                external_application_id="habr-1",
                error=None,
                retryable=False,
            ),
        ),
    }
    world = _make_world(source="habr", adapters=adapters)
    world.job_service.enqueue_for_match(world.match.id)
    worker = _make_worker(world)

    processed = await worker.process_one()

    assert processed is not None
    assert adapters["habr"].call_count == 1
    assert adapters["hh"].call_count == 0
    assert adapters["habr"].submitted == [processed.id]


# ---------------------------------------------------------------------------
# process_one — success path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_process_one_marks_job_complete_on_success() -> None:
    """A successful adapter result transitions the job to ``succeeded``."""
    world = _make_world(
        job_result=ApplyResult(
            success=True,
            external_application_id="hh-app-99",
            error=None,
            retryable=False,
        )
    )
    world.job_service.enqueue_for_match(world.match.id)
    worker = _make_worker(world)

    processed = await worker.process_one()

    assert processed is not None
    assert processed.status == ApplyJobStatus.SUCCEEDED.value
    assert processed.external_application_id == "hh-app-99"
    assert processed.finished_at is not None
    # The job repo is the source of truth — confirm the persisted row.
    stored = world.job_repo.get_by_id(processed.id)
    assert stored is not None
    assert stored.status == ApplyJobStatus.SUCCEEDED.value


@pytest.mark.asyncio
async def test_process_one_updates_match_status_on_success() -> None:
    """A successful submission flips the underlying match to ``applied``."""
    world = _make_world()
    world.job_service.enqueue_for_match(world.match.id)
    worker = _make_worker(world)

    await worker.process_one()

    # Re-fetch through the match service to assert the status flip.
    updated = world.match_service.get(world.match.id, user_id=world.user_id)
    assert updated.status == MatchStatus.APPLIED.value


# ---------------------------------------------------------------------------
# process_one — retryable failure
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_process_one_retries_retryable_failure() -> None:
    """A retryable failure parks the job back in ``queued`` with backoff."""
    world = _make_world(
        job_result=ApplyResult(
            success=False,
            external_application_id=None,
            error="boom",
            retryable=True,
        )
    )
    world.job_service.enqueue_for_match(world.match.id)
    worker = _make_worker(world)
    before = datetime.now(UTC)

    processed = await worker.process_one()

    assert processed is not None
    # The job was retried — still queued, with a future next_run_at and a
    # record of the error.
    assert processed.status == ApplyJobStatus.QUEUED.value
    assert processed.last_error == "boom"
    # ``claim_next`` bumped ``attempts`` to 1; ``mark_attempt`` (called
    # from ``fail``) bumps it to 2. The backoff is computed off the
    # pre-``mark_attempt`` value (1), so the delay is ``2 ** 1 == 2`` s.
    assert processed.attempts == 2
    assert processed.next_run_at is not None
    # Exponential backoff: attempts==1 → 2 ** 1 == 2 seconds.
    delay = (processed.next_run_at - before).total_seconds()
    assert 1.5 <= delay <= 3.0
    # The job is not claimable until next_run_at elapses.
    assert world.job_service.claim_next() is None


@pytest.mark.asyncio
async def test_process_one_marks_dead_letter_on_max_attempts() -> None:
    """A retryable failure after the budget is exhausted parks ``dead_letter``."""
    world = _make_world(
        job_result=ApplyResult(
            success=False,
            external_application_id=None,
            error="boom",
            retryable=True,
        )
    )
    world.job_service.enqueue_for_match(world.match.id)
    # First failure (with the worker bypassed) — bumps attempts to 1.
    job = world.job_service.claim_next()
    assert job is not None
    job = world.job_service.fail(job.id, error="boom", retryable=True)
    # Force attempts to the max-1 boundary so the worker's next failure
    # trips the dead-letter gate. ``update_status`` keeps the row queued.
    job.attempts = DEFAULT_MAX_ATTEMPTS - 1
    world.job_repo.update_status(job.id, ApplyJobStatus.QUEUED.value)
    # Clear the next_run_at so the row is immediately claimable.
    job.next_run_at = None

    worker = _make_worker(world, max_attempts=DEFAULT_MAX_ATTEMPTS)

    processed = await worker.process_one()

    assert processed is not None
    assert processed.status == ApplyJobStatus.DEAD_LETTER.value
    assert processed.last_error == "boom"


# ---------------------------------------------------------------------------
# process_one — non-retryable failure
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_process_one_marks_dead_letter_on_non_retryable() -> None:
    """A non-retryable failure goes straight to ``dead_letter``."""
    world = _make_world(
        job_result=ApplyResult(
            success=False,
            external_application_id=None,
            error="nope",
            retryable=False,
        )
    )
    world.job_service.enqueue_for_match(world.match.id)
    worker = _make_worker(world)

    processed = await worker.process_one()

    assert processed is not None
    assert processed.status == ApplyJobStatus.DEAD_LETTER.value
    assert processed.last_error == "nope"
    assert processed.finished_at is not None


# ---------------------------------------------------------------------------
# process_one — missing adapter for the vacancy's source
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_process_one_marks_dead_letter_for_unknown_source() -> None:
    """No adapter for the vacancy's source → ``dead_letter`` with a stable code."""
    adapters = {
        "hh": _FakeAdapter(
            "hh",
            ApplyResult(
                success=True,
                external_application_id="hh-1",
                error=None,
                retryable=False,
            ),
        )
    }
    world = _make_world(source="habr", adapters=adapters)
    world.job_service.enqueue_for_match(world.match.id)
    worker = _make_worker(world)

    processed = await worker.process_one()

    assert processed is not None
    assert processed.status == ApplyJobStatus.DEAD_LETTER.value
    assert processed.last_error == "no_adapter_for_source"
    assert adapters["hh"].call_count == 0


# ---------------------------------------------------------------------------
# ApplyWorkerProcess — loop / shutdown
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_apply_worker_process_loops_with_poll_interval() -> None:
    """``run`` keeps processing until shutdown, sleeping between iterations."""
    world = _make_world()
    world.job_service.enqueue_for_match(world.match.id)
    worker = _make_worker(world)
    process = ApplyWorkerProcess(worker=worker, poll_interval_seconds=0.05)
    process.start()

    call_count = 0
    original = worker.process_one

    async def tracked() -> object:
        nonlocal call_count
        call_count += 1
        return await original()

    worker.process_one = tracked  # type: ignore[method-assign]

    async def stop_after_three() -> None:
        while call_count < 3:
            await asyncio.sleep(0.01)
        process.stop()

    try:
        start = time.monotonic()
        rc = await asyncio.wait_for(
            asyncio.gather(process.run(), stop_after_three()),
            timeout=2.0,
        )
        elapsed = time.monotonic() - start
    finally:
        process.stop()

    # ``process.run`` is the only task that returns an int; ``gather``'s
    # result is a list — pull the int from the first element.
    assert rc[0] == 0
    assert call_count >= 3
    # Two sleeps between three iterations → at least 2 * 0.05 == 0.1s.
    assert elapsed >= 0.1


@pytest.mark.asyncio
async def test_apply_worker_process_handles_graceful_shutdown() -> None:
    """Setting the shutdown event mid-loop makes ``run`` exit promptly."""
    world = _make_world()
    worker = _make_worker(world)
    process = ApplyWorkerProcess(worker=worker, poll_interval_seconds=0.5)
    process.start()

    # Trigger shutdown almost immediately.
    loop = asyncio.get_running_loop()
    loop.call_later(0.05, process.stop)

    start = time.monotonic()
    rc = await asyncio.wait_for(process.run(), timeout=1.0)
    elapsed = time.monotonic() - start

    assert rc == 0
    # The loop should exit on the first sleep tick after ``stop``.
    assert elapsed < 1.0
