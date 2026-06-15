"""Pydantic DTOs for the ``cover_letter`` slice (M3, issues #31 + #32).

The HTTP / service boundary exchanges :class:`CoverLetterDraftRead`
DTOs. The repository is the only layer that knows the ORM row; the
service maps ORM → DTO and is the only place that decides which fields
are public.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class CoverLetterDraftRead(BaseModel):
    """Public view of a single cover-letter draft (one version).

    The DTO mirrors the ORM row but is intentionally a separate type so
    that the service can choose which fields to expose and so that
    adding internal columns to the model does not accidentally leak
    them to the HTTP layer.
    """

    model_config = ConfigDict(extra="forbid", frozen=False)

    id: uuid.UUID
    match_id: uuid.UUID
    user_id: uuid.UUID
    version: int
    text: str
    style: str | None = None
    user_comment: str | None = None
    generation_prompt_hash: str | None = None
    parent_draft_id: uuid.UUID | None = None
    replaced_by_id: uuid.UUID | None = None
    created_at: datetime
    updated_at: datetime | None = None


class CoverLetterRegenerateRequest(BaseModel):
    """Optional payload for ``POST /cover-letters/regenerate/{match_id}``.

    Both fields are optional: omitting them keeps whatever default the
    generator had on file. The Telegram "regenerate" action will
    typically send a ``user_comment``; the web dashboard can also pin a
    new ``style`` per regeneration.
    """

    model_config = ConfigDict(extra="forbid", frozen=False)

    style: str | None = None
    user_comment: str | None = Field(default=None, max_length=4000)


__all__ = [
    "CoverLetterDraftRead",
    "CoverLetterRegenerateRequest",
]
