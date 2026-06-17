"""FastAPI router for the ``apply_worker`` slice (M5, issue #43).

Endpoints
---------

* ``GET /apply-jobs`` — list the caller's apply jobs.
* ``GET /apply-jobs/limits`` — current per-user rate-limit snapshot
  (M5, issue #46).
* ``GET /apply-jobs/{id}`` — fetch a single apply job (ownership
  enforced).
* ``GET /apply-jobs/{id}/history`` — list the caller's apply job
  history (M5, issue #49); ownership enforced, returns rows in
  chronological order.
* ``POST /apply-jobs/enqueue/{match_id}`` — idempotently enqueue an
  apply job for an accepted match.
* ``POST /apply-jobs/{id}/cancel`` — cancel a queued job.
* ``GET /apply-history`` — combined view of the caller's apply
  history across all of their apply jobs (M6, issue #54); supports
  ``job_id`` and ``status`` filters plus ``limit`` / ``offset``
  pagination.

All endpoints require a valid bearer token, resolved through the
``default_token_store`` configured by the ``users`` slice. The service
is collaborator-injected: tests build it with in-memory fakes,
production wires the SQLAlchemy-backed implementations sharing the
request session.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Sequence

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from apply_pilot.config import get_apply_worker_settings
from apply_pilot.db import get_db
from apply_pilot.features.apply_worker.limits import (
    RateLimiter,
    SqlRateLimiter,
)
from apply_pilot.features.apply_worker.models import ApplyStatusHistory
from apply_pilot.features.apply_worker.repository import (
    SqlApplyJobRepository,
    SqlApplyStatusHistoryRepository,
)
from apply_pilot.features.apply_worker.schemas import (
    ApplyJobRead,
    ApplyRateLimitRead,
    ApplyStatusHistoryList,
    ApplyStatusHistoryRead,
    apply_job_to_dto,
    apply_rate_limit_to_dto,
    apply_status_history_list_to_dto,
    apply_status_history_to_dto,
)
from apply_pilot.features.apply_worker.service import (
    ApplyJobAlreadyTerminalError,
    ApplyJobDependencyMissingError,
    ApplyJobNotFoundError,
    ApplyJobOwnershipError,
    ApplyJobService,
    RateLimitExceeded,
)
from apply_pilot.features.matches.repository import SqlVacancyMatchRepository
from apply_pilot.features.search_profiles.repository import SqlSearchProfileRepository
from apply_pilot.features.users.security import InvalidTokenError, default_token_store

#: Maximum value the dashboard may request for ``GET /apply-history?limit=``.
#: The cap protects the database from a pathologically large page size
#: while still being generous enough for an interactive UI.
APPLY_HISTORY_MAX_LIMIT: int = 200

#: Default page size for ``GET /apply-history`` when the caller does not
#: supply one. Matches the ``list_user_jobs`` default so the dashboard
#: can treat the two endpoints uniformly.
APPLY_HISTORY_DEFAULT_LIMIT: int = 50

_LOGGER = logging.getLogger("apply_pilot.features.apply_worker.api")

router = APIRouter(prefix="/apply-jobs", tags=["apply-jobs"])

# ``auto_error=False`` lets us return our own 401 with a stable JSON
# shape instead of FastAPI's default ``{"detail": "Not authenticated"}``.
_bearer_scheme = HTTPBearer(auto_error=False)


def _http_error(status_code: int, code: str, message: str) -> HTTPException:
    """Return a JSON-shaped 4xx error that the API contract promises."""
    return HTTPException(status_code=status_code, detail={"code": code, "message": message})


def _resolve_user_id(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer_scheme),  # noqa: B008
) -> str:
    """Extract the user id from the bearer token, or raise 401."""
    if credentials is None:
        raise _http_error(
            status.HTTP_401_UNAUTHORIZED,
            "authentication_required",
            "bearer token is required",
        )
    tokens = default_token_store()
    try:
        return tokens.resolve(credentials.credentials)
    except InvalidTokenError as exc:
        raise _http_error(
            status.HTTP_401_UNAUTHORIZED,
            "invalid_token",
            "the supplied token is invalid or expired",
        ) from exc


def get_apply_job_service(
    session: Session = Depends(get_db),  # noqa: B008
) -> ApplyJobService:
    """Build an :class:`ApplyJobService` for the current request.

    The four repositories share the request-scoped session so the
    enqueue / cancel / list / history operations all participate in a
    single transaction. Dependency overrides in the test suite swap the
    in-memory fakes in place of the SQL implementations.

    The retry policy is built from :class:`ApplyWorkerSettings` (loaded
    from ``APP_APPLY_*`` env vars at process start) so the M5 retry
    semantics — exponential backoff, jitter, ``max_attempts`` — apply
    uniformly across HTTP and worker invocations.

    The :class:`RateLimiter` (M5, issue #46) shares the same session
    factory so ``record`` / ``check`` participate in the request
    transaction. The hourly / daily caps are read from
    :class:`ApplyWorkerSettings` so the same env-driven knobs that
    tune the retry policy tune the anti-spam budget.
    """

    def _session_scope() -> Session:
        return session

    job_repo = SqlApplyJobRepository(session_factory=_session_scope)
    match_repo = SqlVacancyMatchRepository(session_factory=_session_scope)
    profile_repo = SqlSearchProfileRepository(session_factory=_session_scope)
    history_repo = SqlApplyStatusHistoryRepository(session_factory=_session_scope)
    rate_limiter: RateLimiter = SqlRateLimiter(
        session_factory=_session_scope,
        settings=get_apply_worker_settings(),
    )
    return ApplyJobService(
        job_repo=job_repo,
        match_repo=match_repo,
        profile_repo=profile_repo,
        history_repo=history_repo,
        retry_policy=get_apply_worker_settings().to_retry_policy(),
        rate_limiter=rate_limiter,
    )


# ---------------------------------------------------------------------------
# Route handlers
# ---------------------------------------------------------------------------


@router.get(
    "",
    response_model=list[ApplyJobRead],
    responses={
        401: {"description": "Missing or invalid bearer token"},
    },
)
def list_apply_jobs(
    user_id_str: str = Depends(_resolve_user_id),  # noqa: B008
    service: ApplyJobService = Depends(get_apply_job_service),  # noqa: B008
) -> list[ApplyJobRead]:
    """List the caller's apply jobs, newest first."""
    jobs = service.list_user_jobs(uuid.UUID(user_id_str))
    return [apply_job_to_dto(j) for j in jobs]


# NOTE: ``/limits`` (M5, issue #46) must be declared *before*
# ``/{job_id}`` so FastAPI's path matcher does not greedily bind
# the literal ``limits`` to the dynamic ``{job_id}`` parameter. The
# same pattern applies to ``/enqueue/{match_id}`` (declared further
# down) for the same reason.


@router.get(
    "/limits",
    response_model=ApplyRateLimitRead,
    responses={
        401: {"description": "Missing or invalid bearer token"},
    },
)
def get_apply_job_limits(
    user_id_str: str = Depends(_resolve_user_id),  # noqa: B008
    service: ApplyJobService = Depends(get_apply_job_service),  # noqa: B008
) -> ApplyRateLimitRead:
    """Return the caller's current rate-limit snapshot (M5, issue #46).

    The endpoint is read-only: it does not consume a token, does not
    touch the queue, and does not require a match to exist. The
    dashboard calls it on page load to render the hourly / daily
    progress bars and the "back in N seconds" countdown.
    """
    result = service.rate_limit_status(uuid.UUID(user_id_str))
    return apply_rate_limit_to_dto(result)


@router.get(
    "/{job_id}",
    response_model=ApplyJobRead,
    responses={
        401: {"description": "Missing or invalid bearer token"},
        403: {"description": "Apply job does not belong to the caller"},
        404: {"description": "Apply job not found"},
    },
)
def get_apply_job(
    job_id: str,
    user_id_str: str = Depends(_resolve_user_id),  # noqa: B008
    service: ApplyJobService = Depends(get_apply_job_service),  # noqa: B008
) -> ApplyJobRead:
    """Return a single apply job, enforcing ownership."""
    try:
        job_uuid = uuid.UUID(job_id)
    except ValueError as exc:
        raise _http_error(status.HTTP_404_NOT_FOUND, "not_found", "invalid job id") from exc
    try:
        job = service.get(job_uuid, user_id=uuid.UUID(user_id_str))
    except ApplyJobNotFoundError as exc:
        raise _http_error(status.HTTP_404_NOT_FOUND, exc.code, exc.message) from exc
    except ApplyJobOwnershipError as exc:
        raise _http_error(status.HTTP_403_FORBIDDEN, exc.code, exc.message) from exc
    return apply_job_to_dto(job)


@router.post(
    "/enqueue/{match_id}",
    response_model=ApplyJobRead,
    status_code=status.HTTP_201_CREATED,
    responses={
        401: {"description": "Missing or invalid bearer token"},
        404: {"description": "Match or search profile not found"},
        429: {"description": "Per-user rate limit exceeded"},
    },
)
def enqueue_apply_job(
    match_id: str,
    user_id_str: str = Depends(_resolve_user_id),  # noqa: B008
    service: ApplyJobService = Depends(get_apply_job_service),  # noqa: B008
) -> ApplyJobRead:
    """Enqueue an apply job for ``match_id`` (idempotent).

    A second call for the same match returns the existing job rather
    than spawning a duplicate. The endpoint accepts any well-formed
    ``match_id``; ownership of the match is enforced indirectly
    through the match's search profile, which the service looks up
    before creating the row.

    The endpoint maps :class:`RateLimitExceeded` (M5, issue #46) to a
    ``429 Too Many Requests`` response. The ``Retry-After`` header
    carries the seconds-until-retry hint from the limiter.
    """
    try:
        match_uuid = uuid.UUID(match_id)
    except ValueError as exc:
        raise _http_error(status.HTTP_404_NOT_FOUND, "not_found", "invalid match id") from exc
    # The user_id is resolved for the auth gate; the service enforces
    # ownership of the match via the search-profile lookup.
    _ = uuid.UUID(user_id_str)
    try:
        job = service.enqueue_for_match(match_uuid)
    except ApplyJobDependencyMissingError as exc:
        raise _http_error(status.HTTP_404_NOT_FOUND, exc.code, str(exc)) from exc
    except RateLimitExceeded as exc:
        # 429 + Retry-After header. The HTTPException detail includes
        # the structured payload the dashboard uses to render the
        # rate-limit card on the apply-jobs page.
        retry_after = exc.retry_after_seconds
        headers = {"Retry-After": str(retry_after)} if retry_after is not None else None
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail={
                "code": exc.code,
                "message": str(exc),
                "retry_after_seconds": retry_after,
            },
            headers=headers,
        ) from exc
    return apply_job_to_dto(job)


@router.get(
    "/{job_id}/history",
    response_model=list[ApplyStatusHistoryRead],
    responses={
        401: {"description": "Missing or invalid bearer token"},
        403: {"description": "Apply job does not belong to the caller"},
        404: {"description": "Apply job not found"},
    },
)
def get_apply_job_history(
    job_id: str,
    user_id_str: str = Depends(_resolve_user_id),  # noqa: B008
    service: ApplyJobService = Depends(get_apply_job_service),  # noqa: B008
) -> list[ApplyStatusHistoryRead]:
    """List the chronological status-transition history for ``job_id`` (M5, #49).

    Ownership is enforced through :meth:`ApplyJobService.list_history`,
    so a missing or foreign job returns the same 404 / 403 codes as the
    other apply-jobs endpoints.
    """
    try:
        job_uuid = uuid.UUID(job_id)
    except ValueError as exc:
        raise _http_error(status.HTTP_404_NOT_FOUND, "not_found", "invalid job id") from exc
    try:
        rows = service.list_history(job_uuid, user_id=uuid.UUID(user_id_str))
    except ApplyJobNotFoundError as exc:
        raise _http_error(status.HTTP_404_NOT_FOUND, exc.code, exc.message) from exc
    except ApplyJobOwnershipError as exc:
        raise _http_error(status.HTTP_403_FORBIDDEN, exc.code, exc.message) from exc
    return [apply_status_history_to_dto(r) for r in rows]


@router.post(
    "/{job_id}/cancel",
    response_model=ApplyJobRead,
    responses={
        401: {"description": "Missing or invalid bearer token"},
        403: {"description": "Apply job does not belong to the caller"},
        404: {"description": "Apply job not found"},
        409: {"description": "Apply job is already in a terminal state"},
    },
)
def cancel_apply_job(
    job_id: str,
    user_id_str: str = Depends(_resolve_user_id),  # noqa: B008
    service: ApplyJobService = Depends(get_apply_job_service),  # noqa: B008
) -> ApplyJobRead:
    """Cancel a queued apply job.

    The transition is allowed only from ``queued`` (or ``failed``).
    A ``succeeded`` / ``dead_letter`` / ``cancelled`` job returns 409
    because the operation cannot succeed.
    """
    try:
        job_uuid = uuid.UUID(job_id)
    except ValueError as exc:
        raise _http_error(status.HTTP_404_NOT_FOUND, "not_found", "invalid job id") from exc
    try:
        job = service.cancel(job_uuid, user_id=uuid.UUID(user_id_str))
    except ApplyJobNotFoundError as exc:
        raise _http_error(status.HTTP_404_NOT_FOUND, exc.code, exc.message) from exc
    except ApplyJobOwnershipError as exc:
        raise _http_error(status.HTTP_403_FORBIDDEN, exc.code, exc.message) from exc
    except ApplyJobAlreadyTerminalError as exc:
        raise _http_error(status.HTTP_409_CONFLICT, exc.code, exc.message) from exc
    return apply_job_to_dto(job)


# ---------------------------------------------------------------------------
# Combined apply-history view (M6, issue #54)
# ---------------------------------------------------------------------------
#
# The dashboard wants a flat, paginated view of every status transition
# across all of the caller's apply jobs. The endpoint lives in its own
# ``APIRouter`` (no prefix) so the public path is ``/apply-history``
# rather than ``/apply-jobs/apply-history``; the rest of the slice's
# per-job endpoints keep their ``/apply-jobs`` prefix. Both routers
# share the same ``get_apply_job_service`` dependency so a single
# override in tests wires the in-memory fakes into both.


apply_history_router = APIRouter(tags=["apply-history"])


@apply_history_router.get(
    "/apply-history",
    response_model=ApplyStatusHistoryList,
    responses={
        401: {"description": "Missing or invalid bearer token"},
    },
)
def list_apply_history(
    user_id_str: str = Depends(_resolve_user_id),  # noqa: B008
    service: ApplyJobService = Depends(get_apply_job_service),  # noqa: B008
    job_id: uuid.UUID | None = Query(  # noqa: B008
        default=None,
        description="Optional job_id filter; narrows the result to a single apply job.",
    ),
    status_filter: str | None = Query(  # noqa: B008
        default=None,
        alias="status",
        description="Optional to_status filter; accepts any ApplyJobStatus value.",
    ),
    limit: int = Query(  # noqa: B008
        default=APPLY_HISTORY_DEFAULT_LIMIT,
        ge=1,
        le=APPLY_HISTORY_MAX_LIMIT,
        description=(
            f"Page size; capped at {APPLY_HISTORY_MAX_LIMIT} so the database is "
            "protected from a pathologically large request."
        ),
    ),
    offset: int = Query(  # noqa: B008
        default=0,
        ge=0,
        description="Number of rows to skip before returning the page.",
    ),
) -> ApplyStatusHistoryList:
    """Return the caller's combined apply history (M6, #54).

    The response is scoped to ``apply_jobs.user_id`` so a caller can
    only ever see their own rows. ``job_id`` and ``status`` are
    optional filters; ``limit`` / ``offset`` paginate the result. The
    total row count is returned alongside the page so the dashboard
    can render a paginator without a second round-trip.
    """
    rows: Sequence[ApplyStatusHistory]
    rows, total = service.list_user_history(
        uuid.UUID(user_id_str),
        job_id=job_id,
        to_status=status_filter,
        limit=limit,
        offset=offset,
    )
    return apply_status_history_list_to_dto(rows, total)


__all__ = [
    "APPLY_HISTORY_DEFAULT_LIMIT",
    "APPLY_HISTORY_MAX_LIMIT",
    "apply_history_router",
    "get_apply_job_service",
    "router",
]
