"""Apply worker vertical slice (M5, issue #43 + #44 + #46).

Public surface
--------------

* :class:`ApplyJob` — ORM model (one row per accepted vacancy match,
  ``UNIQUE(match_id)`` is the slice's contract).
* :class:`ApplyJobStatus` — lifecycle enum.
* :class:`ApplyStatusHistory` — append-only record of every status
  transition (M5, issue #49).
* :class:`ApplyRateLimitEvent` — append-only record of every enqueue
  used by the rate limiter (M5, issue #46).
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
* :class:`ApplyRateLimitRead` / :class:`WindowStatusRead` — public
  DTOs for the rate-limit snapshot (M5, issue #46).
* :class:`ApplyResult` — adapter return shape.
* :class:`ApplyAdapter` — Protocol the apply worker dispatches to.
* :class:`ApplyWorker` — per-iteration worker that drains the queue.
* :class:`ApplyWorkerProcess` — long-running process driving the worker.
* :class:`HHApplyAdapter` — T5 (#246) adapter that bridges :class:`ApplyJob`
  to ``hh_apply.apply_once`` with idempotency + observability hooks.
* :class:`RateLimiter` / :class:`RateLimitResult` / :class:`WindowStatus` /
  :class:`RateLimitExceeded` / :class:`InMemoryRateLimiter` /
  :class:`SqlRateLimiter` — per-user anti-spam cap machinery
  (M5, issue #46).

The slice is consumed by:

* the apply worker process (:class:`ApplyWorkerProcess`, issue #44)
  which calls :meth:`ApplyJobService.claim_next`,
  :meth:`~ApplyJobService.complete`, and
  :meth:`~ApplyJobService.fail`;
* the ``/accept`` Telegram action (M4, issue #41) which calls
  :meth:`ApplyJobService.enqueue_for_match` after the user accepts a
  match;
* the HTTP API (:mod:`api`) which exposes the dashboard / cancel /
  enqueue / history / limits endpoints.
"""

from __future__ import annotations

from apply_pilot.features.apply_worker.hh_adapter import (
    HHApplyAdapter,
    build_default_hh_apply_adapter,
)
from apply_pilot.features.apply_worker.limits import (
    APPLY_KEY,
    DAILY_WINDOW,
    HOURLY_WINDOW,
    InMemoryRateLimiter,
    RateLimiter,
    RateLimitExceeded,
    RateLimitResult,
    SqlRateLimiter,
    WindowStatus,
    default_rate_limiter,
)
from apply_pilot.features.apply_worker.models import (
    ApplyJob,
    ApplyJobStatus,
    ApplyRateLimitEvent,
    ApplyStatusHistory,
    compute_idempotency_key,
)
from apply_pilot.features.apply_worker.repository import (
    ApplyJobRepository,
    ApplyStatusHistoryRepository,
    InMemoryApplyJobRepository,
    InMemoryApplyStatusHistoryRepository,
    SqlApplyJobRepository,
    SqlApplyStatusHistoryRepository,
)
from apply_pilot.features.apply_worker.runtime import (
    DEFAULT_MAX_ATTEMPTS,
    NO_ADAPTER_ERROR,
    VACANCY_NOT_FOUND_ERROR,
    ApplyAdapter,
    ApplyResult,
    ApplyWorker,
    ApplyWorkerProcess,
)
from apply_pilot.features.apply_worker.schemas import (
    ApplyJobRead,
    ApplyRateLimitRead,
    ApplyStatusHistoryRead,
    WindowStatusRead,
    apply_job_to_dto,
    apply_rate_limit_to_dto,
    apply_status_history_to_dto,
)
from apply_pilot.features.apply_worker.service import (
    DEFAULT_RETRY_BACKOFF,
    ApplyJobAlreadyTerminalError,
    ApplyJobDependencyMissingError,
    ApplyJobNotFoundError,
    ApplyJobOwnershipError,
    ApplyJobService,
)

__all__ = [
    "APPLY_KEY",
    "DAILY_WINDOW",
    "DEFAULT_MAX_ATTEMPTS",
    "DEFAULT_RETRY_BACKOFF",
    "HOURLY_WINDOW",
    "HHApplyAdapter",
    "NO_ADAPTER_ERROR",
    "VACANCY_NOT_FOUND_ERROR",
    "ApplyAdapter",
    "ApplyJob",
    "ApplyJobAlreadyTerminalError",
    "ApplyJobDependencyMissingError",
    "ApplyJobNotFoundError",
    "ApplyJobOwnershipError",
    "ApplyJobRead",
    "ApplyJobRepository",
    "ApplyJobService",
    "ApplyJobStatus",
    "ApplyRateLimitEvent",
    "ApplyRateLimitRead",
    "ApplyResult",
    "ApplyStatusHistory",
    "ApplyStatusHistoryRead",
    "ApplyStatusHistoryRepository",
    "ApplyWorker",
    "ApplyWorkerProcess",
    "InMemoryApplyJobRepository",
    "InMemoryApplyStatusHistoryRepository",
    "InMemoryRateLimiter",
    "RateLimitExceeded",
    "RateLimitResult",
    "RateLimiter",
    "SqlApplyJobRepository",
    "SqlApplyStatusHistoryRepository",
    "SqlRateLimiter",
    "WindowStatus",
    "WindowStatusRead",
    "apply_job_to_dto",
    "apply_rate_limit_to_dto",
    "apply_status_history_to_dto",
    "build_default_hh_apply_adapter",
    "compute_idempotency_key",
    "default_rate_limiter",
]
