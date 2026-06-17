"""Pydantic DTOs for the learning-signals slice (M8, issue #63).

The slice exposes exactly one HTTP read surface —
``GET /learning/signals`` — and the only DTO it returns is
:class:`LearningSignalRead`. UUIDs are serialised as their canonical
string form so the response is stable across the SQL and in-memory
implementations.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from job_apply.features.learning.models import LearningSignal


class LearningSignalRead(BaseModel):
    """The wire representation of a single :class:`LearningSignal` row.

    Every field matches the value object's field, with the same
    nullable / non-nullable semantics. ``score`` and
    ``rejection_reason`` are ``None`` when the user did not supply
    them; ``prompt_version`` is ``None`` when the match had never
    been through the LLM scoring pipeline.
    """

    model_config = ConfigDict(extra="forbid", from_attributes=True)

    id: uuid.UUID = Field(..., description="Server-generated UUID for the signal row.")
    user_id: uuid.UUID = Field(..., description="The user that triggered the signal.")
    match_id: uuid.UUID = Field(..., description="The vacancy match the signal refers to.")
    vacancy_id: uuid.UUID = Field(..., description="The vacancy the match referred to.")
    search_profile_id: uuid.UUID = Field(
        ..., description="The search profile the match was produced for."
    )
    rejection_reason: str | None = Field(
        default=None,
        description="Free-form reason the user supplied with the rejection; null if none.",
    )
    prompt_version: str | None = Field(
        default=None,
        description="The prompt version the match was scored with; null if unscored.",
    )
    score: float | None = Field(
        default=None,
        description="The score the match carried at signal time, in [0.0, 100.0].",
    )
    signal_type: str = Field(
        ..., description="The narrow discriminator: 'rejection', 'dismissal', or 'low_score'."
    )
    created_at: datetime = Field(..., description="UTC timestamp the signal was recorded at.")


def learning_signal_to_read(signal: LearningSignal) -> LearningSignalRead:
    """Translate a :class:`LearningSignal` value object to the wire DTO."""
    return LearningSignalRead.model_validate(signal)


__all__ = ["LearningSignalRead", "learning_signal_to_read"]
