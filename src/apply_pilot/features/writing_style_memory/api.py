"""FastAPI router for the ``writing_style_memory`` slice.

Endpoint
--------

* ``GET /writing-style-memory/me`` — return the caller's aggregated
  style summary (or ``None`` when no cover letter has been accepted
  yet).

The endpoint is intentionally read-only. The ingestion pipeline is
wired into the ``/accept`` Telegram action, not the HTTP surface
(issue #66): the API is the observability / debugging window for the
slice, not a write path.
"""

from __future__ import annotations

import logging
import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from apply_pilot.db import get_db
from apply_pilot.features.users.security import InvalidTokenError, default_token_store
from apply_pilot.features.writing_style_memory.repository import SqlStyleMemoryRepository
from apply_pilot.features.writing_style_memory.schemas import StyleMemoryRead
from apply_pilot.features.writing_style_memory.service import StyleMemoryService

_LOGGER = logging.getLogger("apply_pilot.features.writing_style_memory.api")

router = APIRouter(prefix="/writing-style-memory", tags=["writing-style-memory"])

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


def get_style_memory_service(
    session: Session = Depends(get_db),  # noqa: B008
) -> StyleMemoryService:
    """Build a :class:`StyleMemoryService` for the current request."""
    repo = SqlStyleMemoryRepository(session=session)
    return StyleMemoryService(repository=repo)


# ---------------------------------------------------------------------------
# Route handlers
# ---------------------------------------------------------------------------


@router.get(
    "/me",
    response_model=StyleMemoryRead,
    responses={
        401: {"description": "Missing or invalid bearer token"},
    },
)
def get_my_style_memory(
    user_id_str: str = Depends(_resolve_user_id),  # noqa: B008
    service: StyleMemoryService = Depends(get_style_memory_service),  # noqa: B008
) -> StyleMemoryRead:
    """Return the caller's aggregated style summary, or ``None``."""
    user_id = uuid.UUID(user_id_str)
    summary = service.get_aggregated_summary(user_id)
    return StyleMemoryRead(user_id=user_id, aggregated_summary=summary)


__all__ = [
    "get_style_memory_service",
    "router",
]
