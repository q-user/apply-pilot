"""Resumes ORM model.

The ``Resume`` aggregate stores the user-uploaded file, the bytes size,
and the extracted plain text. The primary key is a UUID so we never leak
the count of stored resumes in sequential ids.

The ``user_id`` column is a string-based foreign key to ``users.id`` on
purpose: the ``User`` model is owned by the auth slice (issue #11) and
does not yet exist on ``origin/main`` at the moment of writing. Keeping
the FK as a string lets the resumes slice be merged first without
requiring a co-ordinated refactor. Once the auth slice lands its model,
SQLAlchemy will resolve ``users.id`` against the real table.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text, Uuid, func
from sqlalchemy.orm import Mapped, mapped_column

from job_apply.db import Base


class Resume(Base):
    """A resume uploaded by an authenticated user."""

    __tablename__ = "resumes"

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    #: FK to ``users.id`` (owned by the auth slice, see module docstring).
    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    filename: Mapped[str] = mapped_column(String(512), nullable=False)
    content_type: Mapped[str] = mapped_column(String(127), nullable=False)
    size: Mapped[int] = mapped_column(Integer, nullable=False)
    raw_text: Mapped[str] = mapped_column(Text, nullable=False)
    plain_text: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), onupdate=func.now(), nullable=True
    )


__all__ = ["Resume"]
