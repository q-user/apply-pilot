"""Apply worker vertical slice (M5, issue #43).

Public surface
--------------

* :class:`ApplyJob` — ORM model (one row per accepted vacancy match,
  ``UNIQUE(match_id)`` is the slice's contract).
* :class:`ApplyJobStatus` — lifecycle enum.
* :class:`ApplyStatusHistory` — append-only record of every status
  transition (M5, issue #49).
* :func:`compute_idempotency_key` — SHA-256 of ``(user, vacancy, match)``.
* :class:`ApplyJobRepository` — Protocol contract.
* :class:`ApplyStatusHistoryRepository` — Protocol contract for the
  append-only history stream.
* :class:`InMemoryApplyJobRepository` — fake for tests.
* :class:`InMemoryApplyStatusHistoryRepository` — fake for tests.
* :class:`SqlApplyJobRepository` — production implementation.
* :class:`SqlApplyStatusHistoryRepository` — production implementation.
* :class:`ApplyJobService` — business logic.
* :class:`ApplyJobRead` — public DTO.
* :class:`ApplyStatusHistoryRead` — public DTO for history rows.

The slice is consumed by:

* the apply worker process (background runner, not yet implemented in
  M5) which calls :meth:`ApplyJobService.claim_next`,
  :meth:`~ApplyJobService.complete`, and
  :meth:`~ApplyJobService.fail`;
* the ``/accept`` Telegram action (M4, issue #41) which calls
  :meth:`ApplyJobService.enqueue_for_match` after the user accepts a
  match;
* the HTTP API (:mod:`api`) which exposes the dashboard / cancel /
  enqueue / history endpoints.
"""

from __future__ import annotations

from job_apply.features.apply_worker.models import (
    ApplyJob,
    ApplyJobStatus,
    ApplyStatusHistory,
    compute_idempotency_key,
)
from job_apply.features.apply_worker.repository import (
    ApplyJobRepository,
    ApplyStatusHistoryRepository,
    InMemoryApplyJobRepository,
    InMemoryApplyStatusHistoryRepository,
    SqlApplyJobRepository,
    SqlApplyStatusHistoryRepository,
)
from job_apply.features.apply_worker.schemas import (
    ApplyJobRead,
    ApplyStatusHistoryRead,
    apply_job_to_dto,
    apply_status_history_to_dto,
)
from job_apply.features.apply_worker.service import (
    DEFAULT_RETRY_BACKOFF,
    ApplyJobAlreadyTerminalError,
    ApplyJobDependencyMissingError,
    ApplyJobNotFoundError,
    ApplyJobOwnershipError,
    ApplyJobService,
)

__all__ = [
    "DEFAULT_RETRY_BACKOFF",
    "ApplyJob",
    "ApplyJobAlreadyTerminalError",
    "ApplyJobDependencyMissingError",
    "ApplyJobNotFoundError",
    "ApplyJobOwnershipError",
    "ApplyJobRead",
    "ApplyJobRepository",
    "ApplyJobService",
    "ApplyJobStatus",
    "ApplyStatusHistory",
    "ApplyStatusHistoryRead",
    "ApplyStatusHistoryRepository",
    "InMemoryApplyJobRepository",
    "InMemoryApplyStatusHistoryRepository",
    "SqlApplyJobRepository",
    "SqlApplyStatusHistoryRepository",
    "apply_job_to_dto",
    "apply_status_history_to_dto",
    "compute_idempotency_key",
]
