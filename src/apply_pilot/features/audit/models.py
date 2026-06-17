"""AuditLog ORM model.

Each row represents a single business event (register, login, etc.).
The ``user_id`` column references ``users.id`` logically but no
``ForeignKey`` constraint is enforced so that audit history survives
user deletion.  The column is nullable — anonymous events (e.g. a
failed login for an unknown email) can still be recorded without a
user row.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum

from sqlalchemy import DateTime, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from apply_pilot.db import Base
from apply_pilot.features.users.models import GUID


class AuditEventType(StrEnum):
    """Stable event-type constants used across the system.

    New event types must be added here (not passed as free-form strings)
    so that querying by event type stays predictable.
    """

    REGISTER = "register"
    LOGIN = "login"
    TELEGRAM_LINK = "telegram_link"
    RESUME_UPLOAD = "resume_upload"
    PROFILE_UPDATE = "profile_update"
    VACANCY_MATCH_REJECTED = "vacancy_match_rejected"
    MATCH_ACCEPTED = "match_accepted"
    MATCH_DEFERRED = "match_deferred"
    MATCH_REVIEWED = "match_reviewed"
    COVER_LETTER_REGENERATED = "cover_letter_regenerated"
    SOURCE_DEGRADED = "source_degraded"
    SOURCE_RECOVERED = "source_recovered"


class AuditLog(Base):
    """An append-only audit record for a business event."""

    __tablename__ = "audit_logs"

    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=uuid.uuid4)
    event_type: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(),
        nullable=True,
        index=True,
    )
    details: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"AuditLog(id={self.id!s}, event_type={self.event_type!r}, user_id={self.user_id!s})"


__all__ = ["AuditEventType", "AuditLog"]
