"""Apply worker runtime (M5, issue #44).

The :class:`ApplyWorker` is the per-iteration loop body: claim one
:class:`~apply_pilot.features.apply_worker.models.ApplyJob`, pick the
right adapter, dispatch, and walk the lifecycle (``succeeded`` /
requeued / ``dead_letter``). The :class:`ApplyWorkerProcess` wraps
that loop in a :class:`~apply_pilot.runtime.process.BaseProcess` so the
OS signal handlers are installed and ``asyncio.sleep`` is interleaved
with work.

Slice boundaries
----------------

The runtime is the only place that bridges three slices:

* ``apply_worker`` — :class:`ApplyJobService` for queue operations.
* ``matches`` — :class:`MatchService` for flipping a match to
  ``applied`` once a submission succeeds.
* ``sources`` — a small ``_VacancyLookup`` Protocol the worker uses
  to read the vacancy's ``source`` field (the adapter key).

Adapters are injected as a ``dict[str, ApplyAdapter]`` keyed by the
vacancy's ``source`` (``hh``, ``habr``, ...). Adapters implement the
:class:`ApplyAdapter` Protocol — a ``name`` attribute plus an async
:meth:`ApplyAdapter.submit` that returns an :class:`ApplyResult`.

Retry policy
------------

A retryable failure delegates the backoff schedule to the
:class:`~apply_pilot.features.apply_worker.service.ApplyJobService`,
which forwards to the injected
:class:`~apply_pilot.features.apply_worker.retry.RetryPolicy`. The
worker itself only chooses the log line (``retry`` vs
``dead_letter``) based on its own ``max_attempts`` budget; the
actual ``next_run_at`` is computed by the policy from the
post-``mark_attempt`` attempt count.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import dataclass
from typing import Protocol

from apply_pilot.features.apply_worker.models import ApplyJob
from apply_pilot.features.apply_worker.service import ApplyJobService
from apply_pilot.features.matches.models import MatchStatus
from apply_pilot.features.matches.service import MatchService
from apply_pilot.features.sources.models import Vacancy
from apply_pilot.runtime.process import BaseProcess

#: Maximum number of attempts before a retryable failure is dead-lettered.
#: Three attempts means at most two retries after the first failure.
DEFAULT_MAX_ATTEMPTS: int = 3

#: Stable error code emitted on ``dead_letter`` when no adapter is
#: registered for the vacancy's ``source`` field. The string is the
#: contract — consumers (alerting, dashboards) match on it.
NO_ADAPTER_ERROR: str = "no_adapter_for_source"

#: Stable error code emitted on ``dead_letter`` when the job's
#: ``vacancy_id`` no longer resolves to a row.
VACANCY_NOT_FOUND_ERROR: str = "vacancy_not_found"

_LOG_PREFIX = "apply_pilot.features.apply_worker.runtime."


# ---------------------------------------------------------------------------
# Adapter contract
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ApplyResult:
    """Outcome of an :class:`ApplyAdapter` submission.

    Attributes
    ----------
    success:
        ``True`` when the external system accepted the application.
    external_application_id:
        The id the external system assigned to the application. ``None``
        on failure or when the system did not return one.
    error:
        Human-readable error message; ``None`` on success. Stored on
        the :class:`~apply_pilot.features.apply_worker.models.ApplyJob`
        row as ``last_error`` for the dashboard.
    retryable:
        ``True`` when the worker may park the row back in ``queued``
        and try again. ``False`` short-circuits to ``dead_letter``.
    """

    success: bool
    external_application_id: str | None
    error: str | None
    retryable: bool


class ApplyAdapter(Protocol):
    """Adapter that submits an application for a given match+job.

    The :attr:`name` attribute is informational — the worker uses the
    dict key from the constructor to pick the adapter, but the
    attribute is convenient for structured logging.
    """

    name: str

    async def submit(self, job: ApplyJob) -> ApplyResult: ...


# ---------------------------------------------------------------------------
# Cross-slice Protocol
# ---------------------------------------------------------------------------


class _VacancyLookup(Protocol):
    """The slice's view of the vacancy repository.

    Only :meth:`get_by_id` is needed — the worker reads the vacancy's
    ``source`` to pick the right adapter.
    """

    def get_by_id(self, vacancy_id: uuid.UUID) -> Vacancy | None: ...


# ---------------------------------------------------------------------------
# Worker
# ---------------------------------------------------------------------------


class ApplyWorker:
    """Drain one :class:`ApplyJob` per :meth:`process_one` call.

    The worker is collaborator-injected. Tests build it with the
    in-memory fakes the rest of the slice already uses; production
    wiring in :mod:`apply_pilot.features.apply_worker.api` (or the
    future process entry-point) plugs in the SQLAlchemy-backed
    implementations.
    """

    def __init__(
        self,
        job_service: ApplyJobService,
        match_service: MatchService,
        vacancy_repo: _VacancyLookup,
        adapters: dict[str, ApplyAdapter],
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    ) -> None:
        if max_attempts < 1:
            raise ValueError("max_attempts must be >= 1")
        self._job_service = job_service
        self._match_service = match_service
        self._vacancy_repo = vacancy_repo
        # Copy so callers cannot mutate the dict after construction.
        self._adapters: dict[str, ApplyAdapter] = dict(adapters)
        self._max_attempts = max_attempts
        self._logger = logging.getLogger(f"{_LOG_PREFIX}ApplyWorker")

    @property
    def max_attempts(self) -> int:
        """Return the retry budget for a single :class:`ApplyJob`."""
        return self._max_attempts

    @property
    def adapters(self) -> dict[str, ApplyAdapter]:
        """Return a copy of the adapter registry (read-only snapshot)."""
        return dict(self._adapters)

    # ------------------------------------------------------------------
    # Single iteration
    # ------------------------------------------------------------------

    async def process_one(self) -> ApplyJob | None:
        """Claim and process a single job; return it, or ``None`` on empty queue.

        Flow:

        1. :meth:`ApplyJobService.claim_next` — returns ``None`` when
           the queue is empty.
        2. Resolve the vacancy. A missing row parks the job in
           ``dead_letter`` with :data:`VACANCY_NOT_FOUND_ERROR`.
        3. Pick the adapter by the vacancy's ``source``. An unknown
           source parks the job in ``dead_letter`` with
           :data:`NO_ADAPTER_ERROR`.
        4. ``await adapter.submit(job)`` and walk the lifecycle per
           :class:`ApplyResult`.
        """
        job = self._job_service.claim_next()
        if job is None:
            return None

        self._logger.info(
            "apply_worker.claim",
            extra={
                "event": "apply_worker.claim",
                "job_id": str(job.id),
                "vacancy_id": str(job.vacancy_id),
                "attempts": job.attempts,
            },
        )

        vacancy = self._vacancy_repo.get_by_id(job.vacancy_id)
        if vacancy is None:
            self._logger.warning(
                "apply_worker.vacancy_not_found",
                extra={
                    "event": "apply_worker.vacancy_not_found",
                    "job_id": str(job.id),
                    "vacancy_id": str(job.vacancy_id),
                },
            )
            return self._job_service.fail(
                job.id,
                error=VACANCY_NOT_FOUND_ERROR,
                retryable=False,
            )

        adapter = self._adapters.get(vacancy.source)
        if adapter is None:
            self._logger.warning(
                "apply_worker.no_adapter",
                extra={
                    "event": "apply_worker.no_adapter",
                    "job_id": str(job.id),
                    "source": vacancy.source,
                },
            )
            return self._job_service.fail(
                job.id,
                error=NO_ADAPTER_ERROR,
                retryable=False,
            )

        result = await self._dispatch(adapter, job)
        if result.success:
            return self._handle_success(job, result)
        return self._handle_failure(job, result)

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    async def _dispatch(self, adapter: ApplyAdapter, job: ApplyJob) -> ApplyResult:
        """Run the adapter, normalising exceptions into :class:`ApplyResult`.

        An exception inside the adapter is treated as a retryable
        failure with a generic message — network errors and timeouts
        are the common case, and the existing retry budget bounds the
        damage if the failure is actually a programming error.
        """
        try:
            return await adapter.submit(job)
        except Exception as exc:  # noqa: BLE001 — normalised to ApplyResult
            self._logger.exception(
                "apply_worker.adapter_exception",
                extra={
                    "event": "apply_worker.adapter_exception",
                    "job_id": str(job.id),
                    "adapter": getattr(adapter, "name", "unknown"),
                },
            )
            return ApplyResult(
                success=False,
                external_application_id=None,
                error=f"adapter_exception: {exc}",
                retryable=True,
            )

    def _handle_success(self, job: ApplyJob, result: ApplyResult) -> ApplyJob:
        """Walk a successful submission: complete the job, flip the match."""
        external_id = result.external_application_id or ""
        completed = self._job_service.complete(job.id, external_application_id=external_id)
        # Flip the underlying match to ``applied`` so the dashboard and
        # the audit log see a consistent view.
        self._match_service.update_status(job.match_id, MatchStatus.APPLIED.value)
        self._logger.info(
            "apply_worker.completed",
            extra={
                "event": "apply_worker.completed",
                "job_id": str(job.id),
                "external_application_id": external_id,
            },
        )
        return completed

    def _handle_failure(self, job: ApplyJob, result: ApplyResult) -> ApplyJob:
        """Walk a failed submission: requeue with backoff or ``dead_letter``.

        The actual ``next_run_at`` is computed by the
        :class:`~apply_pilot.features.apply_worker.service.ApplyJobService`
        via its injected
        :class:`~apply_pilot.features.apply_worker.retry.RetryPolicy`; the
        worker only decides which log line to emit.
        """
        error = result.error or "unknown_error"
        if result.retryable and job.attempts < self._max_attempts:
            self._logger.info(
                "apply_worker.retry",
                extra={
                    "event": "apply_worker.retry",
                    "job_id": str(job.id),
                    "attempts": job.attempts,
                },
            )
            return self._job_service.fail(
                job.id,
                error=error,
                retryable=True,
            )
        self._logger.warning(
            "apply_worker.dead_letter",
            extra={
                "event": "apply_worker.dead_letter",
                "job_id": str(job.id),
                "error": error,
                "attempts": job.attempts,
                "max_attempts": self._max_attempts,
                "retryable": result.retryable,
            },
        )
        return self._job_service.fail(
            job.id,
            error=error,
            retryable=False,
        )


# ---------------------------------------------------------------------------
# Process
# ---------------------------------------------------------------------------


class ApplyWorkerProcess(BaseProcess):
    """Long-running :class:`BaseProcess` that drives an :class:`ApplyWorker`.

    Each iteration calls :meth:`ApplyWorker.process_one` and then
    sleeps for :attr:`poll_interval_seconds` (default 5 s). The sleep
    is bounded by the shutdown event — a SIGTERM in the middle of a
    sleep is observed on the next tick of the loop without waiting
    for the full interval.
    """

    def __init__(
        self,
        *,
        worker: ApplyWorker,
        poll_interval_seconds: float = 5.0,
        name: str = "apply-worker",
    ) -> None:
        if poll_interval_seconds <= 0:
            raise ValueError("poll_interval_seconds must be > 0")
        super().__init__(name=name)
        self._worker = worker
        self._poll_interval_seconds = poll_interval_seconds

    @property
    def poll_interval_seconds(self) -> float:
        """Return the sleep duration between :meth:`process_one` calls."""
        return self._poll_interval_seconds

    @property
    def worker(self) -> ApplyWorker:
        """Return the underlying :class:`ApplyWorker` (read-only)."""
        return self._worker

    async def run(self) -> int:
        """Drain the queue until the shutdown event is set.

        Returns 0 on a graceful shutdown. Any exception raised by
        :meth:`ApplyWorker.process_one` is logged and the loop
        continues — a single bad job must not crash the worker.
        """
        self.start()
        try:
            while not self.is_shutdown_set():
                try:
                    await self._worker.process_one()
                except Exception:  # noqa: BLE001 — never crash the worker
                    self._logger.exception(
                        "apply_worker.iteration_error",
                        extra={"event": "apply_worker.iteration_error"},
                    )
                if self.is_shutdown_set():
                    break
                # Sleep with the shutdown event as a cancellation point.
                # ``wait_for`` returns when the event is set; on a clean
                # timeout we go around the loop again.
                try:
                    await asyncio.wait_for(
                        self.wait_for_shutdown(),
                        timeout=self._poll_interval_seconds,
                    )
                    # Event was set during the wait — exit the loop.
                    break
                except TimeoutError:
                    pass
            return 0
        finally:
            self.stop()


__all__ = [
    "DEFAULT_MAX_ATTEMPTS",
    "NO_ADAPTER_ERROR",
    "VACANCY_NOT_FOUND_ERROR",
    "ApplyAdapter",
    "ApplyResult",
    "ApplyWorker",
    "ApplyWorkerProcess",
]
