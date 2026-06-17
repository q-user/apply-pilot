"""ORM model for the cover-letter style preferences slice.

Each user has at most one :class:`CoverLetterStyle` row (uniqueness on
``user_id``). The ``focus_areas`` and ``avoid_phrases`` fields are stored
as JSON-encoded ``TEXT`` to keep the migration portable across sqlite
and PostgreSQL; the service layer is responsible for the
``json.dumps``/``json.loads`` round-trip.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String, Text, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from apply_pilot.db import Base
from apply_pilot.shared.types import GUID


class CoverLetterStyle(Base):
    """Per-user preferences for cover-letter generation.

    Public surface (kept stable for downstream cover-letter slices):

    * ``id``: UUID primary key.
    * ``user_id``: FK to :class:`apply_pilot.features.users.models.User`;
      one row per user, enforced by the unique constraint.
    * ``tone``: high-level voice (``"professional"``, ``"friendly"``,
      ``"concise"``, ``"enthusiastic"``, ``"formal"``).
    * ``length``: target length bucket (``"short"``, ``"medium"``,
      ``"long"``).
    * ``focus_areas``: JSON-encoded list of strings the user wants the
      letter to emphasise (e.g. ``["technical_skills", "teamwork"]``).
    * ``avoid_phrases``: JSON-encoded list of phrases to avoid.
    * ``extra_instructions``: free-form user text. ``None`` when unset.
    * ``created_at`` / ``updated_at``: server-side timestamps.
    """

    __tablename__ = "cover_letter_styles"
    __table_args__ = (UniqueConstraint("user_id", name="uq_cover_letter_styles_user_id"),)

    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        GUID(),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    tone: Mapped[str] = mapped_column(String(32), nullable=False, default="professional")
    length: Mapped[str] = mapped_column(String(16), nullable=False, default="medium")
    focus_areas: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    avoid_phrases: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    extra_instructions: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), onupdate=func.now(), nullable=True
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return (
            f"CoverLetterStyle(id={self.id!s}, user_id={self.user_id!s}, "
            f"tone={self.tone!r}, length={self.length!r})"
        )


__all__ = ["CoverLetterStyle"]
