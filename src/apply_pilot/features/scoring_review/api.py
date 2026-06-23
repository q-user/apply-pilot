"""FastAPI router for the scoring review slice (M8, issue #68).

The router is mounted at ``/admin/scoring-review`` (per the slice's
contract) and exposes two endpoints:

* ``GET  /admin/scoring-review/queue`` — list matches with
  ``confidence < threshold``, ordered by ``confidence ASC``.
* ``POST /admin/scoring-review/{match_id}/note`` — record a reviewer
  note against a match by appending a ``MATCH_REVIEWED`` row to
  ``audit_logs`` (no status change, no schema migration).

Both endpoints require a valid bearer token (issue #145); the gate
honours the ``APP_ADMIN_REQUIRE_AUTH`` env flag (see
:mod:`apply_pilot.features.admin._auth`).
"""

from __future__ import annotations

import logging
import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from apply_pilot.db import get_db
from apply_pilot.features.admin._auth import require_admin_user
from apply_pilot.features.audit.repository import SqlAuditLogRepository
from apply_pilot.features.audit.service import AuditService
from apply_pilot.features.scoring_review.repository import SqlScoringReviewQueue
from apply_pilot.features.scoring_review.schemas import (
    LowConfidenceMatchRead,
    ScoringReviewNoteCreate,
    ScoringReviewNoteResponse,
    low_confidence_match_to_read,
    scoring_review_note_response,
)
from apply_pilot.features.scoring_review.service import ScoringReviewService
from apply_pilot.shared.errors import NotFoundError, ValidationError

_LOGGER = logging.getLogger("apply_pilot.features.scoring_review.api")

router = APIRouter(prefix="/admin/scoring-review", tags=["admin", "scoring-review"])

#: Default confidence threshold for the queue endpoint.
#: Matches with ``confidence < 0.5`` are surfaced for manual review.
DEFAULT_THRESHOLD: float = 0.5

#: Hard cap on the queue endpoint to avoid pulling the whole table.
MAX_LIMIT: int = 200


def _http_error(status_code: int, code: str, message: str) -> HTTPException:
    """Return a JSON-shaped 4xx error that the API contract promises."""
    return HTTPException(status_code=status_code, detail={"code": code, "message": message})


# ---------------------------------------------------------------------------
# Dependency factory
# ---------------------------------------------------------------------------


def get_scoring_review_service(
    session: Session = Depends(get_db),  # noqa: B008
) -> ScoringReviewService:
    """Build a :class:`ScoringReviewService` for the current request.

    The two collaborators (queue and audit) share the request-scoped
    session so the row-existence check and the audit insert see the
    same transaction. Tests override this dependency to inject the
    in-memory fakes.
    """
    queue = SqlScoringReviewQueue(session=session)
    audit_repo = SqlAuditLogRepository(session=session)
    audit_service: AuditService = AuditService(audit_repo=audit_repo)
    return ScoringReviewService(queue=queue, audit_service=audit_service)


# ---------------------------------------------------------------------------
# Route handlers
# ---------------------------------------------------------------------------


@router.get(
    "/queue",
    response_model=list[LowConfidenceMatchRead],
    responses={
        401: {"description": "Missing or invalid bearer token."},
        422: {"description": "Invalid threshold or limit."},
    },
    summary="List matches with low LLM confidence",
)
def list_queue(
    threshold: float = Query(
        default=DEFAULT_THRESHOLD,
        ge=0.0,
        le=1.0,
        description="Maximum confidence; matches with confidence < threshold are listed.",
    ),
    limit: int = Query(
        default=50,
        ge=1,
        le=MAX_LIMIT,
        description="Maximum number of rows to return.",
    ),
    service: ScoringReviewService = Depends(get_scoring_review_service),  # noqa: B008
    _admin_user: str = Depends(require_admin_user),  # noqa: B008
) -> list[LowConfidenceMatchRead]:
    """Return every match with ``confidence < threshold``.

    The result is ordered by ``confidence ASC`` (least confident first)
    so operators see the riskiest rows at the top of the dashboard.
    A ``threshold`` of ``0.5`` is the recommended default — anything
    the LLM is less than half-sure about deserves a human look.
    """
    rows = service.list_low_confidence(threshold, limit=limit)
    return [low_confidence_match_to_read(row) for row in rows]


@router.post(
    "/{match_id}/note",
    response_model=ScoringReviewNoteResponse,
    responses={
        401: {"description": "Missing or invalid bearer token."},
        404: {"description": "The match does not exist."},
        422: {"description": "Note failed validation (empty or too long)."},
    },
    summary="Record a reviewer note against a match",
)
def add_note(
    match_id: str,
    payload: ScoringReviewNoteCreate,
    service: ScoringReviewService = Depends(get_scoring_review_service),  # noqa: B008
    _admin_user: str = Depends(require_admin_user),  # noqa: B008
) -> ScoringReviewNoteResponse:
    """Append a ``MATCH_REVIEWED`` audit event for *match_id*.

    The endpoint does not change the match's status — it is an
    audit-style annotation so operators can leave a human note
    alongside a low-confidence row without forcing a state
    transition. To keep the audit log scannable the note must be
    between 1 and 2000 characters.
    """
    try:
        match_uuid = uuid.UUID(match_id)
    except ValueError as exc:
        raise _http_error(status.HTTP_404_NOT_FOUND, "not_found", "invalid match id") from exc
    try:
        service.mark_reviewed(match_uuid, reviewer_note=payload.note)
    except NotFoundError as exc:
        raise _http_error(status.HTTP_404_NOT_FOUND, exc.code, exc.message) from exc
    except ValidationError as exc:
        raise _http_error(status.HTTP_422_UNPROCESSABLE_ENTITY, exc.code, exc.message) from exc
    return scoring_review_note_response(match_uuid, payload.note)


__all__ = [
    "DEFAULT_THRESHOLD",
    "MAX_LIMIT",
    "add_note",
    "get_scoring_review_service",
    "list_queue",
    "router",
]
