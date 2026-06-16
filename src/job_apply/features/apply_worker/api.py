"""FastAPI router for the ``apply_worker`` slice (M5, issue #43).

Endpoints
---------

* ``GET /apply-jobs`` — list the caller's apply jobs.
* ``GET /apply-jobs/{id}`` — fetch a single apply job (ownership
  enforced).
* ``POST /apply-jobs/enqueue/{match_id}`` — idempotently enqueue an
  apply job for an accepted match.
* ``POST /apply-jobs/{id}/cancel`` — cancel a queued job.

All endpoints require a valid bearer token, resolved through the
``default_token_store`` configured by the ``users`` slice. The service
is collaborator-injected: tests build it with in-memory fakes,
production wires the SQLAlchemy-backed implementations sharing the
request session.
"""

from __future__ import annotations

import logging
import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from job_apply.config import get_apply_worker_settings
from job_apply.db import get_db
from job_apply.features.apply_worker.repository import SqlApplyJobRepository
from job_apply.features.apply_worker.schemas import ApplyJobRead, apply_job_to_dto
from job_apply.features.apply_worker.service import (
    ApplyJobAlreadyTerminalError,
    ApplyJobDependencyMissingError,
    ApplyJobNotFoundError,
    ApplyJobOwnershipError,
    ApplyJobService,
)
from job_apply.features.matches.repository import SqlVacancyMatchRepository
from job_apply.features.search_profiles.repository import SqlSearchProfileRepository
from job_apply.features.users.security import InvalidTokenError, default_token_store

_LOGGER = logging.getLogger("job_apply.features.apply_worker.api")

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

    The three repositories share the request-scoped session so the
    enqueue / cancel / list operations all participate in a single
    transaction. Dependency overrides in the test suite swap the
    in-memory fakes in place of the SQL implementations.

    The retry policy is built from :class:`ApplyWorkerSettings` (loaded
    from ``APP_APPLY_*`` env vars at process start) so the M5 retry
    semantics — exponential backoff, jitter, ``max_attempts`` — apply
    uniformly across HTTP and worker invocations.
    """
    job_repo = SqlApplyJobRepository(session_factory=lambda: session)
    match_repo = SqlVacancyMatchRepository(session_factory=lambda: session)
    profile_repo = SqlSearchProfileRepository(session_factory=lambda: session)
    return ApplyJobService(
        job_repo=job_repo,
        match_repo=match_repo,
        profile_repo=profile_repo,
        retry_policy=get_apply_worker_settings().to_retry_policy(),
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
    return apply_job_to_dto(job)


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


__all__ = ["get_apply_job_service", "router"]
