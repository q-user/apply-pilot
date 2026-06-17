"""Pydantic schemas for the scoring review slice (M8, issue #68).

The :class:`LowConfidenceMatch` dataclass in :mod:`models` is the
in-process contract; these Pydantic models are the wire format for
the ``/admin/scoring-review/...`` endpoints. The conversion is
mechanical and goes through :func:`low_confidence_match_to_read` and
:func:`scoring_review_note_response`.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class LowConfidenceMatchRead(BaseModel):
    """Wire format for a single row of the low-confidence review queue.

    ``from_attributes=True`` lets the API layer pass the frozen
    dataclass straight to :meth:`model_validate`; the explicit
    :func:`low_confidence_match_to_read` keeps the contract in one
    place and is the only place that touches the dataclass.
    """

    model_config = ConfigDict(extra="forbid", frozen=False, from_attributes=True)

    match_id: uuid.UUID = Field(description="Primary key of the underlying match row.")
    vacancy_id: uuid.UUID = Field(description="Vacancy the match points at.")
    user_id: uuid.UUID = Field(
        description="Owner of the search profile the match is tied to.",
    )
    search_profile_id: uuid.UUID = Field(
        description="Search profile that produced the match.",
    )
    score: int | None = Field(
        default=None,
        description="LLM-assigned score in [0, 100]; null when scoring is still pending.",
    )
    confidence: float | None = Field(
        default=None,
        description="LLM's self-reported certainty in [0.0, 1.0].",
    )
    prompt_version: str | None = Field(
        default=None,
        description="Identifier of the prompt that produced the score.",
    )
    explanation: str | None = Field(
        default=None,
        description=(
            "Free-form LLM explanation; null for rows scored before explanations were captured."
        ),
    )
    created_at: datetime = Field(description="UTC timestamp the match row was inserted.")


class ScoringReviewNoteCreate(BaseModel):
    """Body of ``POST /admin/scoring-review/{match_id}/note``.

    The note is stored verbatim in the ``audit_logs.details`` JSON
    column. It is intentionally capped at 2000 characters so the
    audit log stays scannable from a human perspective.
    """

    model_config = ConfigDict(extra="forbid", frozen=False)

    note: str = Field(
        min_length=1,
        max_length=2000,
        description="Free-form reviewer annotation, 1–2000 characters.",
    )


class ScoringReviewNoteResponse(BaseModel):
    """Response body for the ``POST .../note`` endpoint.

    The endpoint confirms the note was recorded by echoing the
    ``match_id``, the ``note`` text, and the ``event_type`` that was
    written to ``audit_logs``. The :attr:`event_type` is fixed to
    ``match_reviewed`` for callers that want to query the audit log
    by type without knowing the enum string.
    """

    model_config = ConfigDict(extra="forbid", frozen=False)

    match_id: uuid.UUID
    note: str
    event_type: str = Field(
        default="match_reviewed",
        description="The audit_logs.event_type value the slice wrote.",
    )


def low_confidence_match_to_read(row: Any) -> LowConfidenceMatchRead:
    """Convert a :class:`LowConfidenceMatch` dataclass to a Pydantic model."""
    return LowConfidenceMatchRead.model_validate(row)


def scoring_review_note_response(match_id: uuid.UUID, note: str) -> ScoringReviewNoteResponse:
    """Build the wire response for the ``POST .../note`` endpoint."""
    return ScoringReviewNoteResponse(match_id=match_id, note=note)


__all__ = [
    "LowConfidenceMatchRead",
    "ScoringReviewNoteCreate",
    "ScoringReviewNoteResponse",
    "low_confidence_match_to_read",
    "scoring_review_note_response",
]
