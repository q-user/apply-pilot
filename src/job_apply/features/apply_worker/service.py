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
* :meth:`fail` — record a failed run. ``retryable=True`` parks the row
  back in ``queued`` with a future ``next_run_at`` computed by the
  injected :class:`~job_apply.features.apply_worker.retry.RetryPolicy`;
  once the policy's ``max_attempts`` is exhausted the row transitions
  to ``dead_letter`` for manual inspection. ``retryable=False``
  short-circuits straight to ``dead_letter``.
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

from job_apply.features.apply_worker.limits import (
    APPLY_KEY,
    RateLimiter,
    RateLimitExceeded,
    RateLimitResult,
    default_rate_limiter,
)
from job_apply.features.apply_worker.models import (
    ApplyJob,
    ApplyJobStatus,
    ApplyStatusHistory,
    compute_idempotency_key,
)
from job_apply.features.apply_worker.notifications import ApplyNotifier
from job_apply.features.apply_worker.repository import (
    ApplyJobRepository,
    ApplyStatusHistoryRepository,
)
from job_apply.features.apply_worker.retry import RetryPolicy
from job_apply.features.matches.models import VacancyMatch
from job_apply.features.search_profiles.models import SearchProfile
from job_apply.shared.errors import ConflictError, NotFoundError

#: A retryable failure is rescheduled this far into the future.
#: The constant is small enough that a backoff cycle is short
#: (60s) but large enough to avoid hammering a flaky upstream.
#:
#: Kept as a module-level constant for backwards compatibility with
#: callers that constructed the service before the retry-policy
#: abstraction was added (M5, issue #47). New code should pass an
#: explicit :class:`RetryPolicy` via the ``retry_policy`` parameter
#: instead of relying on this default.
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
        retry_policy: RetryPolicy | None = None,
        retry_backoff: timedelta = DEFAULT_RETRY_BACKOFF,
        rate_limiter: RateLimiter | None = None,
        notifier: ApplyNotifier | None = None,
    ) -> None:
        self._job_repo = job_repo
        self._match_repo = match_repo
        self._profile_repo = profile_repo
        self._history_repo = history_repo
        # M5 #50 — outbound notifier. When ``None`` (the default) the
        # service preserves its pre-#50 behaviour so callers that
        # have not yet wired a notifier (production runtime, HTTP API
        # before the bot is configured) keep working. When injected,
        # ``complete`` / ``fail`` / ``cancel`` invoke it after the
        # history row is written so the user always sees the final
        # status regardless of which code path produced it.
        self._notifier: ApplyNotifier | None = notifier
        # The ``RateLimiter`` (M5, issue #46) gates ``enqueue_for_match``
        # on a per-user hourly / daily cap so a runaway script (or a
        # user click-spamming ``/accept``) cannot flood hh.ru with
        # submissions. When the caller does not inject a limiter the
        # service falls back to a permissive no-op so existing test
        # fixtures that pre-date the rate-limit feature keep working
        # without modification.
        self._rate_limiter: RateLimiter = rate_limiter or default_rate_limiter()
        # The ``RetryPolicy`` is the modern entry point; ``retry_backoff``
        # is the legacy knob that predates issue #47. When the caller
        # passes an explicit policy we use it as-is. When the caller
        # passes only the legacy ``retry_backoff`` we wrap it in a
        # no-jitter, single-attempt policy so the rest of the code
        # path is uniform.
        if retry_policy is not None:
            self._retry_policy = retry_policy
            self._legacy_retry_backoff: timedelta | None = None
        else:
            self._retry_policy = RetryPolicy(
                max_attempts=999_999,
                base_delay_seconds=retry_backoff.total_seconds(),
                max_delay_seconds=retry_backoff.total_seconds(),
                backoff_multiplier=1.0,
                jitter=False,
            )
            self._legacy_retry_backoff = retry_backoff

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
        """Return the retry-backoff applied on ``fail(retryable=True)``.

        Returns the legacy ``retry_backoff`` constant when the service
        was constructed without an explicit :class:`RetryPolicy`. When a
        policy was injected, the value is derived from its
        ``base_delay_seconds`` so callers that only need a single
        ``timedelta`` keep working.
        """
        if self._legacy_retry_backoff is not None:
            return self._legacy_retry_backoff
        return timedelta(seconds=self._retry_policy.base_delay_seconds)

    # ------------------------------------------------------------------
    # Enqueue
    # ------------------------------------------------------------------

    def enqueue_for_match(self, match_id: uuid.UUID) -> ApplyJob:
        """Create a job for ``match_id`` (or return the existing one).

        The flow:

        1. Resolve the match → user (via search profile) and vacancy.
        2. Check the per-user rate limit (M5, issue #46); a saturated
           window raises :class:`RateLimitExceeded` *before* the row
           is inserted so a denied caller pays no I/O cost.
        3. Look up an existing job for the match and return it when
           one is found — the UNIQUE constraint makes this idempotent.
        4. Otherwise insert a new row with ``status=queued`` and a
           fresh ``idempotency_key``.
        5. Record the enqueue against the rate limiter and write the
           initial :class:`ApplyStatusHistory` row (the only row with
           ``from_status=None``).
        """
        match = self._match_repo.get_by_id(match_id)
        if match is None:
            raise ApplyJobDependencyMissingError(f"vacancy match {match_id} not found")
        profile = self._profile_repo.get_by_id(match.search_profile_id)
        if profile is None:
            raise ApplyJobDependencyMissingError(
                f"search profile {match.search_profile_id} not found for match {match_id}"
            )

        # M5 #46 — enforce the per-user rate limit before any storage
        # work. The check is read-only, so a denied caller pays only
        # the cost of a single ``COUNT(*)`` (or an in-memory list scan
        # in tests). ``APPLY_KEY`` is reserved as a module-level
        # constant on the limits module so the HTTP layer can log
        # the same key the service used.
        result = self._rate_limiter.check(profile.user_id, key=APPLY_KEY)
        if not result.allowed:
            raise RateLimitExceeded(result)

        existing = self._job_repo.get_by_match(match_id)
        if existing is not None:
            # Idempotent return path: the user *did* express an
            # intent to apply, so the call still counts toward the
            # anti-spam budget — a click-spamming client should be
            # blocked even when the underlying state is unchanged.
            self._rate_limiter.record(profile.user_id, key=APPLY_KEY)
            return existing

        job = ApplyJob(
            match_id=match_id,
            user_id=profile.user_id,
            vacancy_id=match.vacancy_id,
            idempotency_key=compute_idempotency_key(profile.user_id, match.vacancy_id, match_id),
        )
        created = self._job_repo.create(job)
        # Record the enqueue *after* the row is persisted so a failed
        # insert (e.g. UNIQUE violation) does not consume a token. The
        # service is single-threaded per call, so the race window
        # between the ``check`` and ``record`` is bounded by the
        # duration of one ``enqueue_for_match`` call.
        self._rate_limiter.record(profile.user_id, key=APPLY_KEY)
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

    def rate_limit_status(self, user_id: uuid.UUID) -> RateLimitResult:
        """Return the current :class:`RateLimitResult` for ``user_id``.

        M5, issue #46. The HTTP ``GET /apply-jobs/limits`` endpoint
        delegates to this helper so the slice keeps a single source of
        truth for the snapshot shape. The call is non-mutating; the
        rate limiter only consults the event log.
        """
        return self._rate_limiter.check(user_id, key=APPLY_KEY)

    # ------------------------------------------------------------------
    # Cancel
    # ------------------------------------------------------------------

    def cancel(self, job_id: uuid.UUID, *, user_id: uuid.UUID) -> ApplyJob:
        """Transition a queued job to ``cancelled``.

        Ownership is enforced. The row is also stamped with
        ``finished_at`` so the dashboard can show the cancellation
        time without re-reading the audit log. The notifier (when
        injected) is invoked after the history row is written so the
        user gets a "🚫 Application cancelled." message regardless
        of whether the transition came from the HTTP layer or the
        worker's reconciliation loop.
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
        self._maybe_notify(updated, ApplyJobStatus.CANCELLED.value)
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
        The notifier (when injected) fires after the history row is
        written so the user always gets the ✅ confirmation.
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
        self._maybe_notify(updated, ApplyJobStatus.SUCCEEDED.value)
        return updated

    def fail(
        self,
        job_id: uuid.UUID,
        *,
        error: str,
        retryable: bool,
    ) -> ApplyJob:
        """Record a failed run.

        ``retryable=True`` parks the row back in ``queued`` with a
        future ``next_run_at`` computed by the configured
        :class:`~job_apply.features.apply_worker.retry.RetryPolicy` so
        the worker picks it up again after the backoff window. Once
        the policy's ``max_attempts`` is exhausted, the row is moved to
        ``dead_letter`` for manual inspection. ``retryable=False``
        short-circuits straight to ``dead_letter``. In all branches
        ``mark_attempt`` increments ``attempts`` and stores
        ``last_error``. The notifier (when injected) is invoked after
        the history row is written: ``failed`` for the retryable
        branch (so the user sees the ❌ retry hint) and
        ``dead_letter`` for the exhausted branch.
        """
        job = self._require_exists(job_id)
        _assert_not_terminal(job)
        from_status = job.status
        self._job_repo.mark_attempt(job_id, error)
        # Re-read so ``attempts`` reflects the value just persisted by
        # ``mark_attempt``. The repository's write is synchronous (in
        # the in-memory fake) and committed (in the SQL implementation)
        # by the time we get here, so a follow-up ``get_by_id`` returns
        # the bumped counter.
        fresh = self._job_repo.get_by_id(job_id)
        attempts = fresh.attempts if fresh is not None else job.attempts
        if retryable and self._retry_policy.should_retry(attempts):
            next_run_at = self._retry_policy.compute_next_run_at(attempts)
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
            self._maybe_notify(updated, ApplyJobStatus.FAILED.value)
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
        self._maybe_notify(updated, ApplyJobStatus.DEAD_LETTER.value)
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

    def _maybe_notify(self, job: ApplyJob, status: str) -> None:
        """Fire the notifier for a terminal transition, if one is wired.

        Centralising the ``is not None`` guard here keeps the three
        transition methods (``complete`` / ``fail`` / ``cancel``) free
        of an extra branch and means a future caller that wants the
        same notification can just invoke this helper. A notifier
        that raises is allowed to bubble up — failing to deliver a
        Telegram message must not silently mask a buggy integration,
        and the upstream worker / request handler logs and surfaces
        the exception to the operator.
        """
        if self._notifier is None:
            return
        self._notifier.notify(job.user_id, job=job, status=status)


__all__ = [
    "ApplyJobAlreadyTerminalError",
    "ApplyJobDependencyMissingError",
    "ApplyJobNotFoundError",
    "ApplyJobOwnershipError",
    "ApplyJobService",
    "DEFAULT_RETRY_BACKOFF",
]
