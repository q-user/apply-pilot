"""FastAPI router for the cover_letter_style slice.

Endpoints
---------

* ``GET /cover-letter-style`` — get my style (or default if none).
* ``PUT /cover-letter-style`` — upsert (full update with full style).
* ``DELETE /cover-letter-style`` — remove (idempotent).

All endpoints require a valid bearer token; the user id is derived
from the token. There is no path parameter because each user has at
most one style.
"""

from __future__ import annotations

import logging
import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from job_apply.db import get_db
from job_apply.features.cover_letter_style.repository import (
    SqlCoverLetterStyleRepository,
)
from job_apply.features.cover_letter_style.schemas import (
    CoverLetterStyleRead,
    CoverLetterStyleUpdate,
)
from job_apply.features.cover_letter_style.service import CoverLetterStyleService
from job_apply.features.users.security import InvalidTokenError, default_token_store
from job_apply.shared.errors import ValidationError

_LOGGER = logging.getLogger("job_apply.features.cover_letter_style.api")

router = APIRouter(prefix="/cover-letter-style", tags=["cover-letter-style"])

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


def get_cover_letter_style_service(
    session: Session = Depends(get_db),  # noqa: B008
) -> CoverLetterStyleService:
    """Build a ``CoverLetterStyleService`` for the current request."""
    repo = SqlCoverLetterStyleRepository(session=session)
    return CoverLetterStyleService(repo)


# ---------------------------------------------------------------------------
# Route handlers
# ---------------------------------------------------------------------------


@router.get(
    "",
    response_model=CoverLetterStyleRead,
    responses={
        401: {"description": "Missing or invalid bearer token"},
    },
)
def get_my_style(
    user_id_str: str = Depends(_resolve_user_id),  # noqa: B008
    service: CoverLetterStyleService = Depends(get_cover_letter_style_service),  # noqa: B008
) -> CoverLetterStyleRead:
    """Return the caller's cover-letter style, or a default if unset."""
    return service.get_or_default(uuid.UUID(user_id_str))


@router.put(
    "",
    response_model=CoverLetterStyleRead,
    responses={
        401: {"description": "Missing or invalid bearer token"},
        422: {"description": "Validation error"},
    },
)
def upsert_my_style(
    payload: CoverLetterStyleUpdate,
    user_id_str: str = Depends(_resolve_user_id),  # noqa: B008
    service: CoverLetterStyleService = Depends(get_cover_letter_style_service),  # noqa: B008
) -> CoverLetterStyleRead:
    """Create or update the caller's cover-letter style."""
    try:
        return service.upsert(uuid.UUID(user_id_str), payload)
    except ValidationError as exc:
        raise _http_error(status.HTTP_422_UNPROCESSABLE_ENTITY, exc.code, exc.message) from exc


@router.delete(
    "",
    status_code=status.HTTP_204_NO_CONTENT,
    responses={
        401: {"description": "Missing or invalid bearer token"},
    },
)
def delete_my_style(
    user_id_str: str = Depends(_resolve_user_id),  # noqa: B008
    service: CoverLetterStyleService = Depends(get_cover_letter_style_service),  # noqa: B008
) -> None:
    """Delete the caller's cover-letter style (idempotent)."""
    service.delete(uuid.UUID(user_id_str))
    return None


__all__ = [
    "get_cover_letter_style_service",
    "router",
]
