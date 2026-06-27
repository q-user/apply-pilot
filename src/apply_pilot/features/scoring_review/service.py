"""Business logic for the scoring review slice (M8, issue #68).

The :class:`ScoringReviewService` is a thin orchestrator in front of a
:class:`ScoringReviewQueue` and the :class:`AuditService`. The slice is
read-mostly: ``list_low_confidence`` is a pass-through, and the only
write (``mark_reviewed``) validates the match exists, then emits a
``MATCH_REVIEWED`` audit event whose ``details`` JSON carries the
reviewer note. The match itself is not mutated — the note is an
audit-style annotation, not a status change.

The service is collaborator-injected: tests build it with the
in-memory queue and the in-memory audit fake, the FastAPI dependency
in :mod:`api` builds it with the SQLAlchemy-backed implementations
sharing the request's session.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import datetime
from typing import Protocol

from apply_pilot.features.audit.models import AuditEventType
from apply_pilot.features.scoring_review.models import LowConfidenceMatch
from apply_pilot.shared.errors import NotFoundError

# ---------------------------------------------------------------------------
# Protocol the service depends on (audit slice, narrowed to one method)
# ---------------------------------------------------------------------------


class _AuditLogger(Protocol):
    """Subset of :class:`AuditService` the service actually uses.

    Declared locally so the service does not have to import the full
    :class:`AuditService` and so test fakes can supply a stand-in.
    """

    def log_event(
        self,
        event_type: AuditEventType,
        user_id: uuid.UUID | None = None,
        details: dict[str, object] | None = None,
    ) -> None: ...


class ScoringReviewService:
    """Orchestrate the queue + audit calls for the admin review slice.

    The service is intentionally a thin layer: every method is a single
    collaborator call plus a small amount of business logic. The
    :class:`ScoringReviewService` does not enforce any new business
    rules; the queue already validates that the match exists, and the
    audit slice is the source of truth for the event.
    """

    def __init__(self, *, queue: object, audit_service: _AuditLogger) -> None:
        self._queue = queue
        self._audit_service = audit_service

    @property
    def queue(self) -> object:
        """Expose the queue for tests that need to assert on its state."""
        return self._queue

    def list_low_confidence(
        self,
        threshold: float,
        *,
        limit: int = 50,
        since: datetime | None = None,
    ) -> Sequence[LowConfidenceMatch]:
        """Return matches with ``confidence < threshold``, ordered by confidence ASC."""
        return self._queue.list_low_confidence(threshold, limit, since)  # type: ignore

    def mark_reviewed(self, match_id: uuid.UUID, *, reviewer_note: str) -> None:
        """Record a reviewer note against a match.

        The queue is asked to validate the match exists; the service
        then writes an :attr:`AuditEventType.MATCH_REVIEWED` event
        whose ``details`` JSON captures both ``match_id`` and ``note``
        for later inspection. The match's own status is intentionally
        not changed — notes are audit-style annotations, not state
        transitions.
        """
        # Validate the match exists. The queue raises
        # :class:`NotFoundError` for unknown ids; we re-raise as-is
        # so the API layer can map it to a 404.
        try:
            self._queue.mark_reviewed(match_id)  # type: ignore
        except NotFoundError:
            raise
        self._audit_service.log_event(
            AuditEventType.MATCH_REVIEWED,
            user_id=None,
            details={"match_id": str(match_id), "note": reviewer_note},
        )


__all__ = ["ScoringReviewService"]
