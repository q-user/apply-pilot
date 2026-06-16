"""Business logic for the ``apply_worker`` slice (M5, issue #43).

The :class:`ApplyJobService` owns the queue's lifecycle. It exposes:

* :meth:`enqueue_for_match` — idempotent enqueue used by the
  ``/accept`` telegram action (issue #41) and the HTTP API. The
  ``UNIQUE(match_id)`` constraint is the storage-layer contract; the
  service performs a pre-insert lookup so the common case returns the
  existing row without raising an integrity error.
* :meth:`claim_next` — called by the worker to atomically pick the
  next claimable row and transition it to ``running``.
* :meth:`complete` — record a successful hh submission (storing the
  ``external_application_id``).
* :meth:`fail` — record a failed run. ``retryable=True`` parks the job
  back in ``queued`` with a future ``next_run_at``; ``retryable=False``
  transitions to ``dead_letter`` for manual inspection.
* :meth:`cancel` — user-initiated cancellation; only valid from
  ``queued`` (or ``failed``) states.
* :meth:`get` / :meth:`list_user_jobs` — read-through helpers that
  enforce ownership.

The service is collaborator-injected. The cross-slice dependencies
(matches / search profiles) are typed as :class:`Protocol` so the
slice does not import their concrete repositories. Production wiring
in :mod:`api` plugs in the SQLAlchemy-backed implementations.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

from job_apply.features.apply_worker.models import (
    ApplyJob,
    ApplyJobStatus,
    ApplyStatusHistory,
    compute_idempotency_key,
)
from job_apply.features.apply_worker.repository import (
    ApplyJobRepository,
    ApplyStatusHistoryRepository,
)
from job_apply.features.matches.models import VacancyMatch
from job_apply.features.search_profiles.models import SearchProfile
from job_apply.shared.errors import ConflictError, NotFoundError

#: A retryable failure is rescheduled this far into the future.
#: The constant is small enough that a backoff cycle is short
#: (60s) but large enough to avoid hammering a flaky upstream.
DEFAULT_RETRY_BACKOFF: timedelta = timedelta(seconds=60)

#: A job is considered "terminal" (no further transitions allowed
#: other than manual inspection) when its status is one of these.
_TERMINAL_STATUSES: frozenset[str] = frozenset(
    {
        ApplyJobStatus.SUCCEEDED.value,
        ApplyJobStatus.CANCELLED.value,
        ApplyJobStatus.DEAD_LETTER.value,
    }
)

#: A job can be cancelled from any of these non-terminal states. The
#: ``running`` state is intentionally excluded — the worker is already
#: in flight and the right response to a cancel is to wait for it to
#: finish (and ``fail`` the row if the user changed their mind), not
#: to mid-flight abort.
_CANCELLABLE_STATUSES: frozenset[str] = frozenset(
    {
        ApplyJobStatus.QUEUED.value,
        ApplyJobStatus.FAILED.value,
    }
)


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class ApplyJobNotFoundError(NotFoundError):
    """The requested :class:`ApplyJob` does not exist."""

    code: str = "apply_job_not_found"


class ApplyJobOwnershipError(Exception):
    """The caller does not own the requested :class:`ApplyJob`.

    Raised as a plain ``Exception`` (not :class:`DomainError`) so the
    HTTP layer always returns 403 regardless of error-code evolution.
    """

    code: str = "forbidden"

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class ApplyJobDependencyMissingError(LookupError):
    """A cross-slice lookup the service depends on returned ``None``.

    A missing match or search profile is treated as a programmer
    error — every call site is expected to verify the precondition
    before invoking the service. The error is raised eagerly so a
    wiring mistake never silently produces a row with ``NULL`` foreign
    keys.
    """

    code: str = "apply_job_dependency_missing"


class ApplyJobAlreadyTerminalError(ConflictError):
    """The caller asked to mutate a job that has reached a terminal state.

    ``succeeded`` / ``cancelled`` / ``dead_letter`` are final; the only
    acceptable follow-up is a manual re-queue (out of scope for M5).
    """

    code: str = "apply_job_already_terminal"


# ---------------------------------------------------------------------------
# Cross-slice Protocol types
# ---------------------------------------------------------------------------


class _MatchLookup(Protocol):
    """The slice's view of the vacancy match repository."""

    def get_by_id(self, match_id: uuid.UUID) -> VacancyMatch | None: ...


class _ProfileLookup(Protocol):
    """The slice's view of the search-profile repository."""

    def get_by_id(self, profile_id: uuid.UUID) -> SearchProfile | None: ...


# ---------------------------------------------------------------------------
# Mapping
# ---------------------------------------------------------------------------


def _assert_not_terminal(job: ApplyJob) -> None:
    """Raise :class:`ApplyJobAlreadyTerminalError` if the row is terminal."""
    if job.status in _TERMINAL_STATUSES:
        raise ApplyJobAlreadyTerminalError(
            f"apply job {job.id} is already in terminal state {job.status!r}"
        )


def _assert_cancellable(job: ApplyJob) -> None:
    """Raise :class:`ApplyJobAlreadyTerminalError` if the row cannot be cancelled."""
    if job.status in _TERMINAL_STATUSES:
        raise ApplyJobAlreadyTerminalError(
            f"apply job {job.id} is in terminal state {job.status!r}"
        )
    if job.status not in _CANCELLABLE_STATUSES:
        raise ApplyJobAlreadyTerminalError(
            f"apply job {job.id} is in state {job.status!r} and cannot be cancelled"
        )


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------


class ApplyJobService:
    """Orchestrate the :class:`ApplyJob` lifecycle."""

    def __init__(
        self,
        *,
        job_repo: ApplyJobRepository,
        match_repo: _MatchLookup,
        profile_repo: _ProfileLookup,
        history_repo: ApplyStatusHistoryRepository,
        retry_backoff: timedelta = DEFAULT_RETRY_BACKOFF,
    ) -> None:
        self._job_repo = job_repo
        self._match_repo = match_repo
        self._profile_repo = profile_repo
        self._history_repo = history_repo
        self._retry_backoff = retry_backoff

    @property
    def job_repo(self) -> ApplyJobRepository:
        """Expose the repository for tests that need to assert state."""
        return self._job_repo

    @property
    def history_repo(self) -> ApplyStatusHistoryRepository:
        """Expose the history repository for tests that inspect transitions."""
        return self._history_repo

    @property
    def retry_backoff(self) -> timedelta:
        """Return the retry-backoff applied on ``fail(retryable=True)``."""
        return self._retry_backoff

    # ------------------------------------------------------------------
    # Enqueue
    # ------------------------------------------------------------------

    def enqueue_for_match(self, match_id: uuid.UUID) -> ApplyJob:
        """Create a job for ``match_id`` (or return the existing one).

        The flow:

        1. Resolve the match → user (via search profile) and vacancy.
        2. Look up an existing job for the match and return it when
           one is found — the UNIQUE constraint makes this idempotent.
        3. Otherwise insert a new row with ``status=queued`` and a
           fresh ``idempotency_key``.
        4. Write the initial :class:`ApplyStatusHistory` row (the only
           row with ``from_status=None``).
        """
        match = self._match_repo.get_by_id(match_id)
        if match is None:
            raise ApplyJobDependencyMissingError(f"vacancy match {match_id} not found")
        profile = self._profile_repo.get_by_id(match.search_profile_id)
        if profile is None:
            raise ApplyJobDependencyMissingError(
                f"search profile {match.search_profile_id} not found for match {match_id}"
            )

        existing = self._job_repo.get_by_match(match_id)
        if existing is not None:
            return existing

        job = ApplyJob(
            match_id=match_id,
            user_id=profile.user_id,
            vacancy_id=match.vacancy_id,
            idempotency_key=compute_idempotency_key(profile.user_id, match.vacancy_id, match_id),
        )
        created = self._job_repo.create(job)
        self._record_transition(
            job=created,
            from_status=None,
            to_status=ApplyJobStatus.QUEUED.value,
        )
        return created

    # ------------------------------------------------------------------
    # Read
    # ------------------------------------------------------------------

    def get(self, job_id: uuid.UUID, *, user_id: uuid.UUID) -> ApplyJob:
        """Return a single job, enforcing ownership."""
        job = self._job_repo.get_by_id(job_id)
        if job is None:
            raise ApplyJobNotFoundError(f"apply job {job_id} not found")
        if job.user_id != user_id:
            raise ApplyJobOwnershipError(f"apply job {job_id} does not belong to user {user_id}")
        return job

    def list_user_jobs(
        self,
        user_id: uuid.UUID,
        *,
        limit: int = 50,
    ) -> Sequence[ApplyJob]:
        """List the caller's jobs, newest first."""
        return self._job_repo.list_by_user(user_id, limit=limit)

    def list_history(
        self, job_id: uuid.UUID, *, user_id: uuid.UUID
    ) -> Sequence[ApplyStatusHistory]:
        """Return the caller's job history in chronological order.

        Ownership is enforced by re-using :meth:`get` so the HTTP layer
        can map a missing job to 404 and a foreign job to 403 with the
        same errors as the other endpoints.
        """
        self.get(job_id, user_id=user_id)
        return self._history_repo.list_by_job(job_id)

    # ------------------------------------------------------------------
    # Cancel
    # ------------------------------------------------------------------

    def cancel(self, job_id: uuid.UUID, *, user_id: uuid.UUID) -> ApplyJob:
        """Transition a queued job to ``cancelled``.

        Ownership is enforced. The row is also stamped with
        ``finished_at`` so the dashboard can show the cancellation
        time without re-reading the audit log.
        """
        job = self.get(job_id, user_id=user_id)
        _assert_cancellable(job)
        from_status = job.status
        updated = self._job_repo.update_status(
            job_id,
            ApplyJobStatus.CANCELLED.value,
            finished_at=datetime.now(UTC),
        )
        self._record_transition(
            job=updated,
            from_status=from_status,
            to_status=ApplyJobStatus.CANCELLED.value,
        )
        return updated

    # ------------------------------------------------------------------
    # Worker-facing transitions
    # ------------------------------------------------------------------

    def claim_next(self) -> ApplyJob | None:
        """Atomically claim the oldest claimable row.

        Returns ``None`` when the queue is empty. The repository is
        responsible for the actual transition; the service writes the
        matching :class:`ApplyStatusHistory` row so the timeline stays
        consistent with the row's ``status`` field.
        """
        claimed = self._job_repo.claim_next()
        if claimed is None:
            return None
        self._record_transition(
            job=claimed,
            from_status=ApplyJobStatus.QUEUED.value,
            to_status=ApplyJobStatus.RUNNING.value,
        )
        return claimed

    def complete(self, job_id: uuid.UUID, *, external_application_id: str) -> ApplyJob:
        """Record a successful hh submission.

        The row transitions to ``succeeded`` and the application id is
        stored for traceability. ``last_error`` is cleared so a row
        that was retried successfully does not carry stale error text.
        """
        job = self._require_exists(job_id)
        _assert_not_terminal(job)
        from_status = job.status
        updated = self._finish(
            job_id,
            status=ApplyJobStatus.SUCCEEDED.value,
            external_application_id=external_application_id,
        )
        self._record_transition(
            job=updated,
            from_status=from_status,
            to_status=ApplyJobStatus.SUCCEEDED.value,
        )
        return updated

    def fail(self, job_id: uuid.UUID, *, error: str, retryable: bool) -> ApplyJob:
        """Record a failed run.

        ``retryable=True`` parks the row back in ``queued`` with a
        future ``next_run_at`` so the worker picks it up again after
        the backoff window. ``retryable=False`` parks the row in
        ``dead_letter`` for manual inspection. In both branches
        ``mark_attempt`` increments ``attempts`` and stores
        ``last_error``.
        """
        job = self._require_exists(job_id)
        _assert_not_terminal(job)
        from_status = job.status
        self._job_repo.mark_attempt(job_id, error)
        if retryable:
            next_run_at = datetime.now(UTC) + self._retry_backoff
            updated = self._job_repo.update_status(
                job_id,
                ApplyJobStatus.QUEUED.value,
                next_run_at=next_run_at,
            )
            self._record_transition(
                job=updated,
                from_status=from_status,
                to_status=ApplyJobStatus.QUEUED.value,
                error=error,
                metadata={
                    "retryable": True,
                    "attempts": updated.attempts,
                    "next_run_at": next_run_at.isoformat(),
                },
            )
            return updated
        updated = self._finish(
            job_id,
            status=ApplyJobStatus.DEAD_LETTER.value,
        )
        self._record_transition(
            job=updated,
            from_status=from_status,
            to_status=ApplyJobStatus.DEAD_LETTER.value,
            error=error,
            metadata={"retryable": False, "attempts": updated.attempts},
        )
        return updated

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _require_exists(self, job_id: uuid.UUID) -> ApplyJob:
        """Return the row or raise :class:`ApplyJobNotFoundError`."""
        job = self._job_repo.get_by_id(job_id)
        if job is None:
            raise ApplyJobNotFoundError(f"apply job {job_id} not found")
        return job

    def _finish(
        self,
        job_id: uuid.UUID,
        *,
        status: str,
        external_application_id: str | None = None,
    ) -> ApplyJob:
        """Stamp ``finished_at`` on a row and update its status.

        The repository's ``update_status`` writes the supplied
        ``finished_at`` so the lifecycle boundary is persisted in
        the same transaction as the status flip.
        """
        return self._job_repo.update_status(
            job_id,
            status,
            external_application_id=external_application_id,
            finished_at=datetime.now(UTC),
        )

    def _record_transition(
        self,
        *,
        job: ApplyJob,
        from_status: str | None,
        to_status: str,
        error: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        """Append one :class:`ApplyStatusHistory` row for a status change.

        The helper centralises the (from_status, to_status, error,
        metadata_json) shape so every transition in the service writes
        a row that the dashboard can read uniformly. ``metadata`` is
        JSON-encoded into the ``metadata_json`` column; callers pass
        the dict and the storage layer takes the string.
        """
        row = ApplyStatusHistory(
            job_id=job.id,
            from_status=from_status,
            to_status=to_status,
            error=error,
            metadata_json=json.dumps(metadata) if metadata is not None else None,
        )
        self._history_repo.create(row)


__all__ = [
    "ApplyJobAlreadyTerminalError",
    "ApplyJobDependencyMissingError",
    "ApplyJobNotFoundError",
    "ApplyJobOwnershipError",
    "ApplyJobService",
    "DEFAULT_RETRY_BACKOFF",
]
