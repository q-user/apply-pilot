"""ORM model for the ``cover_letter`` slice (M3, issues #31 + #32).

A :class:`CoverLetterDraft` is one version of a cover letter for a
:class:`VacancyMatch`. Each ``match_id`` can have **many** drafts — the
version-history feature added in issue #32 stores every regeneration
as a new row rather than mutating the previous one in place. The
``(match_id, version)`` composite index accelerates the "latest
version" lookup that powers the Telegram / web review surface.

Fields
------

* ``id``               — UUID primary key.
* ``match_id``         — FK to :class:`vacancy_matches.id`. **Not
                        unique** on its own; the version-history is
                        keyed by ``(match_id, version)``.
* ``user_id``          — FK to :class:`users.id`. Duplicated for cheap
                        ownership checks (the API never needs to join
                        through ``vacancy_matches`` to decide whether
                        the caller can see a draft).
* ``version``          — 1 for the first draft, increments by one with
                        every regeneration. Stable for the lifetime of
                        the row.
* ``text``             — the generated cover-letter body (markdown is
                        allowed; the renderer lives downstream).
* ``style``            — the style key the generator was invoked with
                        (e.g. ``"friendly"``). ``None`` means the
                        generator used its default.
* ``user_comment``     — the human hint that triggered a regeneration
                        (e.g. ``"make it warmer"``). ``None`` on the
                        first draft.
* ``parent_draft_id``  — FK to :class:`cover_letter_drafts.id`. The
                        draft this one was regenerated from. ``None``
                        on version 1.
* ``replaced_by_id``   — FK to :class:`cover_letter_drafts.id`. The
                        draft that replaced this one. ``None`` on the
                        latest version.
* ``generation_prompt_hash`` — SHA-256 hex of the (style, comment,
                        match-scoped facts) tuple that produced the
                        text. For audit / debugging when the generator
                        is upgraded.
* ``created_at`` / ``updated_at`` — server-side timestamps.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from job_apply.db import Base
from job_apply.shared.types import GUID


class CoverLetterDraft(Base):
    """A single version of a cover letter for a ``VacancyMatch``.

    See module docstring for field semantics. The
    ``(match_id, version desc)`` composite index keeps the
    "latest for this match" and "history for this match" queries cheap
    as the table grows.
    """

    __tablename__ = "cover_letter_drafts"
    __table_args__ = (
        Index(
            "ix_cover_letter_drafts_match_version",
            "match_id",
            "version",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=uuid.uuid4)
    match_id: Mapped[uuid.UUID] = mapped_column(
        GUID(),
        ForeignKey("vacancy_matches.id", ondelete="CASCADE"),
        nullable=False,
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        GUID(),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    style: Mapped[str | None] = mapped_column(String(32), nullable=True)
    user_comment: Mapped[str | None] = mapped_column(Text, nullable=True)
    generation_prompt_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)

    parent_draft_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(),
        ForeignKey("cover_letter_drafts.id", ondelete="SET NULL"),
        nullable=True,
    )
    replaced_by_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(),
        ForeignKey("cover_letter_drafts.id", ondelete="SET NULL"),
        nullable=True,
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), onupdate=func.now(), nullable=True
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return (
            f"CoverLetterDraft(id={self.id!s}, match_id={self.match_id!s}, "
            f"version={self.version}, parent={self.parent_draft_id!s}, "
            f"replaced_by={self.replaced_by_id!s})"
        )


__all__ = ["CoverLetterDraft"]
