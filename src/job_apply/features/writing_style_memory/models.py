"""ORM model and frozen DTOs for the ``writing_style_memory`` slice.

This slice stores a per-user history of cover letters the user has
accepted, plus a deterministic ``style_summary`` for each one. It is a
**learned** layer on top of the static :mod:`cover_letter_style`
preferences: every time the user accepts a match with a cover letter, a
:class:`StyleMemoryEntry` is appended to the user's memory and can be
read back through the API.

Schema
------

* :class:`StyleMemoryEntryModel` — the SQLAlchemy ORM table. One row
  per accepted cover letter; there is no uniqueness constraint on
  ``(user_id, cover_letter_id)`` because a user may legitimately
  re-accept a match and we want the full history (the M3 #31 model
  upserts the draft in place).
* :class:`StyleMemoryEntry` — frozen dataclass used by the in-memory
  repository and the service layer. It is the public DTO that the
  service exchanges with callers; the ORM row is a persistence detail
  the repository owns.
* :class:`StyleMemory` — frozen dataclass for the read-side: the user
  id, the list of recent entries, and the precomputed aggregated
  summary string.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from job_apply.db import Base
from job_apply.shared.types import GUID

# ---------------------------------------------------------------------------
# ORM model
# ---------------------------------------------------------------------------


class StyleMemoryEntryModel(Base):
    """SQLAlchemy ORM row for a single ``StyleMemoryEntry``.

    The table is append-only: there is no ``updated_at`` column. A
    re-acceptance of the same match produces a new row; the service
    layer is free to choose how to read back the most recent entry.

    ``letter_text`` is stored in full so future "list of accepted
    letters" views can re-derive summaries from a single source of
    truth. ``style_summary`` is the deterministic, MVP summary that the
    slice produces at write time — it is the primary surface the API
    exposes today, so callers do not need a second pass to compute it.
    """

    __tablename__ = "style_memory_entries"

    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        GUID(),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    cover_letter_id: Mapped[uuid.UUID] = mapped_column(
        GUID(),
        ForeignKey("cover_letter_drafts.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    letter_text: Mapped[str] = mapped_column(Text, nullable=False)
    style_summary: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return (
            f"StyleMemoryEntryModel(id={self.id!s}, user_id={self.user_id!s}, "
            f"cover_letter_id={self.cover_letter_id!s})"
        )


# ---------------------------------------------------------------------------
# Frozen DTOs (the slice's public surface)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class StyleMemoryEntry:
    """A single style-memory entry — the public DTO.

    ``letter_text`` is the raw accepted cover letter. ``style_summary``
    is the deterministic summary derived by
    :func:`job_apply.features.writing_style_memory.summariser.summarise_letter`
    (LLM-based summarisation is a follow-up; the storage pipeline does
    not depend on the source of the summary).
    """

    id: uuid.UUID
    user_id: uuid.UUID
    cover_letter_id: uuid.UUID | None
    letter_text: str
    style_summary: str
    created_at: datetime


@dataclass(frozen=True)
class StyleMemory:
    """The aggregate of a user's style memory.

    ``entries`` is the most recent batch (size controlled by the
    repository's ``limit``). ``aggregated_summary`` is the precomputed
    concatenation the API serves; ``None`` means the user has not
    accepted any letter yet.
    """

    user_id: uuid.UUID
    entries: list[StyleMemoryEntry] = field(default_factory=list)
    aggregated_summary: str | None = None


__all__ = [
    "StyleMemory",
    "StyleMemoryEntry",
    "StyleMemoryEntryModel",
]
