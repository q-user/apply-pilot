"""VacancyMatch ORM model.

A :class:`VacancyMatch` is the join row that connects a canonical
:class:`Vacancy` to a :class:`SearchProfile`. Every match carries a
``status`` that drives the downstream review / apply pipeline:

* ``new``       — initial state right after the ingest pipeline
                  produced the match.
* ``scored``    — a scoring pass assigned ``score`` and updated the
                  match (rank ready for review).
* ``review``    — flagged for human review.
* ``accepted``  — the user accepted the match; eligible for applying.
* ``rejected``  — the user rejected the match.
* ``applied``   — the apply pipeline submitted an application.
* ``dismissed`` — the user explicitly hid the match (does not
                  influence scoring).

The ``(search_profile_id, vacancy_id)`` pair is unique: the same
vacancy may be re-ingested under a new status, but it cannot spawn
two separate match rows for the same profile.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum

from sqlalchemy import (
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from job_apply.db import Base
from job_apply.shared.types import GUID


class MatchStatus(StrEnum):
    """Stable set of lifecycle states for a vacancy match.

    Persisted as ``String(50)`` on the model; using a ``StrEnum`` keeps
    the public DTOs and the ``MatchService`` validation aligned.
    """

    NEW = "new"
    SCORED = "scored"
    REVIEW = "review"
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    APPLIED = "applied"
    DISMISSED = "dismissed"


class VacancyMatch(Base):
    """A link between a :class:`Vacancy` and a :class:`SearchProfile`."""

    __tablename__ = "vacancy_matches"
    __table_args__ = (
        UniqueConstraint(
            "search_profile_id",
            "vacancy_id",
            name="uq_vacancy_matches_profile_vacancy",
        ),
        Index(
            "ix_vacancy_matches_profile_status",
            "search_profile_id",
            "status",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=uuid.uuid4)

    search_profile_id: Mapped[uuid.UUID] = mapped_column(
        GUID(),
        ForeignKey("search_profiles.id", ondelete="CASCADE"),
        nullable=False,
    )
    vacancy_id: Mapped[uuid.UUID] = mapped_column(
        GUID(),
        ForeignKey("vacancies.id", ondelete="CASCADE"),
        nullable=False,
    )

    status: Mapped[str] = mapped_column(
        String(50),
        nullable=False,
        default=MatchStatus.NEW.value,
        server_default=MatchStatus.NEW.value,
    )
    score: Mapped[int | None] = mapped_column(Integer, nullable=True)
    match_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    #: LLM prompt template version that produced the score, when one
    #: is known. Populated by the scoring pipeline (issue #29); kept
    #: here so a match row carries the full provenance of its score.
    prompt_version: Mapped[str | None] = mapped_column(String(64), nullable=True)
    #: The LLM's confidence in the score, in ``[0.0, 1.0]``. ``None``
    #: until the scoring pipeline has run.
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    #: When the scoring pipeline last produced a verdict for this
    #: match. ``None`` while the match is still in the ``"new"`` or
    #: ``"review"`` state.

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), onupdate=func.now(), nullable=True
    )
    scored_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return (
            f"VacancyMatch(id={self.id!s}, search_profile_id={self.search_profile_id!s}, "
            f"vacancy_id={self.vacancy_id!s}, status={self.status!r})"
        )


__all__ = ["MatchStatus", "VacancyMatch"]
