"""FastAPI router for the learning-signals slice (M8, issue #63).

Single endpoint — ``GET /learning/signals?user_id=<uuid>&limit=<n>`` —
returns the structured learning signals captured by the rest of the
app. The endpoint is intentionally unauthenticated: it is an
operational / internal surface (mirrors the digest ``POST /digest/send``
style — see ``src/apply_pilot/features/telegram/digest/api.py``), not a
user-facing button. A future slice will guard it behind an admin role.

The ``user_id`` query parameter is required so a typo returns ``422``
instead of leaking every signal in the table. ``limit`` defaults to
``100`` and is capped at ``500``; both bounds are validated by FastAPI.
"""

from __future__ import annotations

import logging
import uuid

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from apply_pilot.db import get_db
from apply_pilot.features.learning.repository import SqlLearningSignalRepository
from apply_pilot.features.learning.schemas import (
    LearningSignalRead,
    learning_signal_to_read,
)
from apply_pilot.features.learning.service import LearningSignalsService

_LOGGER = logging.getLogger("apply_pilot.features.learning.api")

router = APIRouter(prefix="/learning", tags=["learning"])


def get_learning_signals_service(
    session: Session = Depends(get_db),  # noqa: B008
) -> LearningSignalsService:
    """FastAPI dependency: build a :class:`LearningSignalsService` for the request.

    The repository shares the request-scoped session so the read sees
    the same transaction the rest of the request sees. Tests can
    override this dependency to inject the in-memory fake.
    """
    repo = SqlLearningSignalRepository(session=session)
    return LearningSignalsService(repo=repo)


# ---------------------------------------------------------------------------
# Route handler
# ---------------------------------------------------------------------------


@router.get(
    "/signals",
    response_model=list[LearningSignalRead],
    responses={422: {"description": "Invalid query parameters"}},
)
def list_learning_signals(
    user_id: uuid.UUID = Query(  # noqa: B008
        ...,
        description="UUID of the user whose learning signals to read.",
    ),
    limit: int = Query(  # noqa: B008
        default=100,
        ge=1,
        le=500,
        description="Maximum number of signals to return (1-500, default 100).",
    ),
    service: LearningSignalsService = Depends(get_learning_signals_service),  # noqa: B008
) -> list[LearningSignalRead]:
    """Return learning signals for ``user_id``, newest first.

    The response is a JSON array of :class:`LearningSignalRead` DTOs;
    an empty list is returned for users with no signals.
    """
    signals = service.list_for_user(user_id, limit=limit)
    return [learning_signal_to_read(s) for s in signals]


__all__ = ["get_learning_signals_service", "router"]
