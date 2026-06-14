"""Audit use-case service.

The ``AuditService`` is a fire-and-forget logger consumed by other
slices via FastAPI DI. It accepts an ``AuditLogRepository`` by
constructor injection so tests can swap in the in-memory fake.
"""

from __future__ import annotations

import json
import logging
import uuid

from fastapi import Depends
from sqlalchemy.orm import Session

from job_apply.db import get_db
from job_apply.features.audit.models import AuditEventType
from job_apply.features.audit.repository import AuditLogRepository, SqlAuditLogRepository

_LOGGER = logging.getLogger("job_apply.features.audit.service")


def get_audit_service(session: Session = Depends(get_db)) -> "AuditService":  # noqa: B008
    """FastAPI dependency: build an AuditService for the current request.

    Uses the request-scoped session from ``get_db``. Tests can override
    this dependency to inject an in-memory fake.
    """
    repo = SqlAuditLogRepository(session=session)
    return AuditService(audit_repo=repo)


class AuditService:
    """Fire-and-forget audit event logger.

    Every call to ``log_event`` writes an append-only row. The method
    returns ``None`` — callers never wait for or inspect the result.
    """

    def __init__(self, audit_repo: AuditLogRepository) -> None:
        self._audit_repo = audit_repo

    def log_event(
        self,
        event_type: AuditEventType,
        user_id: uuid.UUID | None = None,
        details: dict[str, object] | None = None,
    ) -> None:
        """Record an audit event.

        Args:
            event_type: One of the ``AuditEventType`` enum values.
            user_id: The user that triggered the event, or ``None`` for
                anonymous events.
            details: Optional free-form metadata (serialised as JSON text).
        """
        details_text: str | None = None
        if details:
            details_text = json.dumps(details, default=str, ensure_ascii=False)
        try:
            self._audit_repo.insert(
                event_type=event_type,
                user_id=user_id,
                details=details_text,
            )
        except Exception:
            _LOGGER.exception("audit.log_event.failed", extra={"event_type": event_type})

    @property
    def audit_repo(self) -> AuditLogRepository:
        """Expose the repository for tests that need to assert state."""
        return self._audit_repo


__all__ = ["AuditService", "get_audit_service"]
