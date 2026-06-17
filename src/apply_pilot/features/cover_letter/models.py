"""ORM model for the ``cover_letter`` slice (M3, issue #31).

A :class:`CoverLetterDraft` is the **first** cover letter generated for
a :class:`VacancyMatch`. The ``match_id`` column is ``UNIQUE`` — there
is exactly one draft per match. The follow-up issue (#32) introduces
the version-history workflow that lifts this uniqueness constraint;
until then, re-generating a draft mutates the existing row in place.

Fields
------

* ``id``              — UUID primary key.
* ``match_id``        — FK to :class:`vacancy_matches.id`. ``UNIQUE``:
                        one draft per match. Cascades on delete so a
                        removed match leaves no orphan drafts.
* ``user_id``         — FK to :class:`users.id`. Duplicated for cheap
                        ownership checks; the slice never has to join
                        through ``vacancy_matches`` to decide whether
                        the caller can see a draft.
* ``content``         — the generated cover-letter body (markdown is
                        allowed; the renderer lives downstream).
* ``prompt_version``  — the ``<name>@<semver>`` stamp from the
                        :class:`PromptVersionRegistry` (or the
                        service-level default when no registry is
                        injected).
* ``model_used``      — the LLM model name that produced ``content``.
                        ``None`` when the client does not expose a
                        model attribute.
* ``status``          — one of :class:`CoverLetterDraftStatus`. The
                        slice always creates rows in ``"draft"``;
                        downstream slices can move them through
                        ``"final"`` / ``"sent"`` / ``"archived"``.
* ``created_at`` / ``updated_at`` — server-side timestamps.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from apply_pilot.db import Base
from apply_pilot.shared.types import GUID


class CoverLetterDraftStatus(StrEnum):
    """Lifecycle states for a :class:`CoverLetterDraft`.

    The set is intentionally stable: new states are additive, and
    renaming a state is a breaking change for any consumer that
    matches the string value.

    The M3 #31 slice only ever creates rows in :attr:`DRAFT`. The
    follow-up issue (#32) flips the status to :attr:`REGENERATING`
    while a regeneration is in flight; the other states
    (:attr:`FINAL`, :attr:`SENT`, :attr:`ARCHIVED`) are reserved for
    the review / apply pipeline.
    """

    DRAFT = "draft"
    REGENERATING = "regenerating"
    FINAL = "final"
    SENT = "sent"
    ARCHIVED = "archived"


class CoverLetterDraft(Base):
    """The first — and for #31, only — cover letter for a match.

    See the module docstring for field semantics.
    """

    __tablename__ = "cover_letter_drafts"

    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=uuid.uuid4)
    match_id: Mapped[uuid.UUID] = mapped_column(
        GUID(),
        ForeignKey("vacancy_matches.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        GUID(),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    content: Mapped[str] = mapped_column(Text, nullable=False)
    prompt_version: Mapped[str] = mapped_column(String(50), nullable=False)
    model_used: Mapped[str | None] = mapped_column(String(100), nullable=True)
    # ``version`` tracks the regeneration count. The first draft is
    # ``1``; every successful call to
    # :meth:`CoverLetterService.regenerate_for_match` bumps it. The
    # ``server_default`` mirrors the column default so SQL inserts
    # without an explicit value land at version ``1``.
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")
    status: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        default=CoverLetterDraftStatus.DRAFT.value,
        server_default=CoverLetterDraftStatus.DRAFT.value,
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), onupdate=func.now(), nullable=True
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return (
            f"CoverLetterDraft(id={self.id!s}, match_id={self.match_id!s}, status={self.status!r})"
        )


__all__ = ["CoverLetterDraft", "CoverLetterDraftStatus"]
