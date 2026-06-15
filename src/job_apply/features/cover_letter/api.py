"""FastAPI router for the ``cover_letter`` slice (M3, issues #31 + #32).

Endpoints
---------

* ``GET /cover-letters/by-match/{match_id}`` — return the latest draft
  for a match.
* ``GET /cover-letters/by-match/{match_id}/history`` — return every
  draft for a match, newest version first.
* ``POST /cover-letters/regenerate/{match_id}`` — create a new version
  (or the very first one when no drafts exist). Accepts an optional
  ``CoverLetterRegenerateRequest`` body with ``style`` /
  ``user_comment`` hints.

All endpoints require a valid bearer token; the user id is derived
from the token. The service enforces ownership through the
``user_id`` it receives from the router — each draft is stamped with
the calling user.
"""

from __future__ import annotations

import logging
import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from job_apply.db import get_db
from job_apply.features.cover_letter.generator import (
    CoverLetterGenerator,
    StubCoverLetterGenerator,
)
from job_apply.features.cover_letter.repository import SqlCoverLetterDraftRepository
from job_apply.features.cover_letter.schemas import (
    CoverLetterDraftRead,
    CoverLetterRegenerateRequest,
)
from job_apply.features.cover_letter.service import CoverLetterService
from job_apply.features.users.security import InvalidTokenError, default_token_store

_LOGGER = logging.getLogger("job_apply.features.cover_letter.api")

router = APIRouter(prefix="/cover-letters", tags=["cover-letters"])

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


def get_cover_letter_generator() -> CoverLetterGenerator:
    """Build the cover-letter generator for the current request.

    The default is the network-free :class:`StubCoverLetterGenerator`;
    production wiring is expected to override this dependency with
    the LLM-backed implementation from issue #31. Keeping the seam
    dependency-driven means tests can drop in a fake without touching
    the route handlers.
    """
    return StubCoverLetterGenerator()


def get_cover_letter_service(
    session: Session = Depends(get_db),  # noqa: B008
    generator: CoverLetterGenerator = Depends(get_cover_letter_generator),  # noqa: B008
) -> CoverLetterService:
    """Build a :class:`CoverLetterService` for the current request.

    The SQL repository shares the request-scoped session so the
    regeneration round-trip (insert new draft, update previous
    draft's ``replaced_by_id``) is one transaction.
    """
    repo = SqlCoverLetterDraftRepository(session=session)
    return CoverLetterService(repo, generator)


# ---------------------------------------------------------------------------
# Route handlers
# ---------------------------------------------------------------------------


@router.get(
    "/by-match/{match_id}",
    response_model=CoverLetterDraftRead,
    responses={
        401: {"description": "Missing or invalid bearer token"},
        404: {"description": "No cover-letter drafts for this match"},
    },
)
def get_latest_draft_for_match(
    match_id: str,
    user_id_str: str = Depends(_resolve_user_id),  # noqa: B008
    service: CoverLetterService = Depends(get_cover_letter_service),  # noqa: B008
) -> CoverLetterDraftRead:
    """Return the latest cover-letter draft for ``match_id``."""
    try:
        match_uuid = uuid.UUID(match_id)
    except ValueError as exc:
        raise _http_error(status.HTTP_404_NOT_FOUND, "not_found", "invalid match id") from exc
    latest = service.get_latest_for_match(match_uuid, user_id=uuid.UUID(user_id_str))
    if latest is None:
        raise _http_error(
            status.HTTP_404_NOT_FOUND,
            "cover_letter_draft_not_found",
            "no cover-letter drafts for this match",
        )
    return latest


@router.get(
    "/by-match/{match_id}/history",
    response_model=list[CoverLetterDraftRead],
    responses={
        401: {"description": "Missing or invalid bearer token"},
    },
)
def get_history_for_match(
    match_id: str,
    user_id_str: str = Depends(_resolve_user_id),  # noqa: B008
    service: CoverLetterService = Depends(get_cover_letter_service),  # noqa: B008
) -> list[CoverLetterDraftRead]:
    """Return every cover-letter draft for ``match_id``, newest first."""
    try:
        match_uuid = uuid.UUID(match_id)
    except ValueError as exc:
        raise _http_error(status.HTTP_404_NOT_FOUND, "not_found", "invalid match id") from exc
    return service.get_history_for_match(match_uuid, user_id=uuid.UUID(user_id_str))


@router.post(
    "/regenerate/{match_id}",
    response_model=CoverLetterDraftRead,
    responses={
        401: {"description": "Missing or invalid bearer token"},
    },
)
def regenerate_draft_for_match(
    match_id: str,
    payload: CoverLetterRegenerateRequest | None = None,
    user_id_str: str = Depends(_resolve_user_id),  # noqa: B008
    service: CoverLetterService = Depends(get_cover_letter_service),  # noqa: B008
) -> CoverLetterDraftRead:
    """Create a new (or first) cover-letter draft for ``match_id``.

    Accepts an optional JSON body with ``style`` and ``user_comment``
    hints. The first call for a match creates version 1; subsequent
    calls create version n+1 and back-link the previous draft.
    """
    try:
        match_uuid = uuid.UUID(match_id)
    except ValueError as exc:
        raise _http_error(status.HTTP_404_NOT_FOUND, "not_found", "invalid match id") from exc
    style = payload.style if payload is not None else None
    comment = payload.user_comment if payload is not None else None
    return service.regenerate_for_match(
        match_uuid,
        user_id=uuid.UUID(user_id_str),
        style=style,
        user_comment=comment,
    )


__all__ = [
    "get_cover_letter_generator",
    "get_cover_letter_service",
    "router",
]
