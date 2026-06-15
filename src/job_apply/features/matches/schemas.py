"""DTOs for the matches slice.

The slice uses a single read DTO plus a small update payload. Inputs
are deliberately thin: ``MatchService`` owns validation rules, status
enumeration enforcement, and ownership checks.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from job_apply.features.matches.models import MatchStatus


class VacancyMatchRead(BaseModel):
    """Public read shape for a vacancy match."""

    model_config = ConfigDict(extra="forbid", frozen=False)

    id: uuid.UUID
    search_profile_id: uuid.UUID
    vacancy_id: uuid.UUID
    status: str
    score: int | None = None
    match_reason: str | None = None
    explanation: str | None = None
    prompt_version: str | None = None
    scored_at: datetime | None = None
    created_at: datetime
    updated_at: datetime | None = None


class VacancyMatchStatusUpdate(BaseModel):
    """Payload for ``PATCH /matches/{id}/status``.

    ``score`` is optional: callers updating a match from the
    ``scored`` workflow can attach the score in the same call, while
    status-only transitions leave it untouched.
    """

    model_config = ConfigDict(extra="forbid", frozen=False)

    status: MatchStatus = Field(
        description="New lifecycle state for the match.",
    )
    score: int | None = Field(default=None, ge=0, le=100)


__all__ = ["VacancyMatchRead", "VacancyMatchStatusUpdate"]
