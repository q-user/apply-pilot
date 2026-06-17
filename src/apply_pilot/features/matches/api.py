"""FastAPI router for the matches slice.

Endpoints
---------

* ``GET /matches`` — list the caller's matches (``?status=new``).
* ``GET /matches/{id}`` — get a single match (ownership enforced).
* ``PATCH /matches/{id}/status`` — transition a match to a new status
  (optionally attaching a score).

All endpoints require a valid bearer token, resolved through the
``default_token_store`` configured by the ``users`` slice.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from apply_pilot.db import get_db
from apply_pilot.features.matches.repository import SqlVacancyMatchRepository
from apply_pilot.features.matches.schemas import VacancyMatchRead, VacancyMatchStatusUpdate
from apply_pilot.features.matches.service import (
    MatchNotFoundError,
    MatchOwnershipError,
    MatchService,
)
from apply_pilot.features.search_profiles.repository import SqlSearchProfileRepository
from apply_pilot.features.users.security import InvalidTokenError, default_token_store
from apply_pilot.shared.errors import ValidationError

_LOGGER = logging.getLogger("apply_pilot.features.matches.api")

router = APIRouter(prefix="/matches", tags=["matches"])

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


def get_match_service(
    session: Session = Depends(get_db),  # noqa: B008
) -> MatchService:
    """Build a :class:`MatchService` for the current request.

    Both repositories share the request-scoped session so the API call
    participates in a single transaction.
    """
    match_repo = SqlVacancyMatchRepository(session_factory=lambda: session)
    profile_repo = SqlSearchProfileRepository(session_factory=lambda: session)
    return MatchService(match_repo=match_repo, profile_repo=profile_repo)


# ---------------------------------------------------------------------------
# Route handlers
# ---------------------------------------------------------------------------


@router.get(
    "",
    response_model=list[VacancyMatchRead],
    responses={
        401: {"description": "Missing or invalid bearer token"},
        422: {"description": "Validation error"},
    },
)
def list_matches(
    status_filter: str | None = Query(default=None, alias="status"),
    user_id_str: str = Depends(_resolve_user_id),  # noqa: B008
    service: MatchService = Depends(get_match_service),  # noqa: B008
) -> list[VacancyMatchRead]:
    """List the authenticated user's matches, optionally filtered by status."""
    import uuid

    try:
        return service.list_matches(uuid.UUID(user_id_str), status=status_filter)
    except ValidationError as exc:
        raise _http_error(status.HTTP_422_UNPROCESSABLE_ENTITY, exc.code, exc.message) from exc


@router.get(
    "/{match_id}",
    response_model=VacancyMatchRead,
    responses={
        401: {"description": "Missing or invalid bearer token"},
        403: {"description": "Match does not belong to the caller"},
        404: {"description": "Match not found"},
    },
)
def get_match(
    match_id: str,
    user_id_str: str = Depends(_resolve_user_id),  # noqa: B008
    service: MatchService = Depends(get_match_service),  # noqa: B008
) -> VacancyMatchRead:
    """Return a single match by id, enforcing ownership."""
    import uuid

    try:
        match_uuid = uuid.UUID(match_id)
    except ValueError as exc:
        raise _http_error(status.HTTP_404_NOT_FOUND, "not_found", "invalid match id") from exc
    try:
        return service.get(match_uuid, user_id=uuid.UUID(user_id_str))
    except MatchNotFoundError as exc:
        raise _http_error(status.HTTP_404_NOT_FOUND, exc.code, exc.message) from exc
    except MatchOwnershipError as exc:
        raise _http_error(status.HTTP_403_FORBIDDEN, exc.code, exc.message) from exc


@router.patch(
    "/{match_id}/status",
    response_model=VacancyMatchRead,
    responses={
        401: {"description": "Missing or invalid bearer token"},
        403: {"description": "Match does not belong to the caller"},
        404: {"description": "Match not found"},
        422: {"description": "Validation error"},
    },
)
def update_match_status(
    match_id: str,
    payload: VacancyMatchStatusUpdate,
    user_id_str: str = Depends(_resolve_user_id),  # noqa: B008
    service: MatchService = Depends(get_match_service),  # noqa: B008
) -> VacancyMatchRead:
    """Transition a match to a new status (optionally attaching a score)."""
    import uuid

    try:
        match_uuid = uuid.UUID(match_id)
    except ValueError as exc:
        raise _http_error(status.HTTP_404_NOT_FOUND, "not_found", "invalid match id") from exc
    try:
        return service.update_status(
            match_uuid,
            payload.status.value,
            user_id=uuid.UUID(user_id_str),
            score=payload.score,
        )
    except MatchNotFoundError as exc:
        raise _http_error(status.HTTP_404_NOT_FOUND, exc.code, exc.message) from exc
    except MatchOwnershipError as exc:
        raise _http_error(status.HTTP_403_FORBIDDEN, exc.code, exc.message) from exc
    except ValidationError as exc:
        raise _http_error(status.HTTP_422_UNPROCESSABLE_ENTITY, exc.code, exc.message) from exc


__all__ = ["get_match_service", "router"]
