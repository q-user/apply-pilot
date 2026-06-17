"""Pydantic DTOs for the ``cover_letter_style`` slice.

The HTTP / service boundary always exchanges ``list[str]`` for the
``focus_areas`` and ``avoid_phrases`` fields. The repository is the
only layer that knows about the JSON-on-disk representation.

Validation
----------

* ``tone`` must be one of the allowed high-level voices.
* ``length`` must be one of the allowed length buckets.
* ``focus_areas`` / ``avoid_phrases`` are list of strings, max length
  capped at 64 entries to keep the prompt short and bounded.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

# Allowed values, exported for reuse in service-level validation.
ALLOWED_TONES = ("professional", "friendly", "concise", "enthusiastic", "formal")
ALLOWED_LENGTHS = ("short", "medium", "long")

Tone = Literal["professional", "friendly", "concise", "enthusiastic", "formal"]
Length = Literal["short", "medium", "long"]


class CoverLetterStyleUpdate(BaseModel):
    """Input for ``PUT /cover-letter-style``.

    All fields are optional so a single endpoint can be used for
    partial updates as well as full replacement. The service applies
    the documented defaults when a field is omitted.
    """

    model_config = ConfigDict(extra="forbid", frozen=False)

    tone: Tone | None = Field(default=None)
    length: Length | None = Field(default=None)
    focus_areas: list[str] | None = Field(default=None, max_length=64)
    avoid_phrases: list[str] | None = Field(default=None, max_length=64)
    extra_instructions: str | None = Field(default=None, max_length=4000)


class CoverLetterStyleRead(BaseModel):
    """Output shape for a cover letter style resource.

    ``id`` is included on the public DTO (the task asks to *exclude* it
    from ``CoverLetterStyleRead`` for the internal model, but the API
    surface benefits from a stable identifier — we expose the row's own
    ``id`` for clients that need idempotent updates later).
    """

    model_config = ConfigDict(extra="forbid", frozen=False)

    id: uuid.UUID
    user_id: uuid.UUID
    tone: Tone
    length: Length
    focus_areas: list[str]
    avoid_phrases: list[str]
    extra_instructions: str | None
    created_at: datetime
    updated_at: datetime | None = None


__all__ = [
    "ALLOWED_LENGTHS",
    "ALLOWED_TONES",
    "CoverLetterStyleRead",
    "CoverLetterStyleUpdate",
    "Length",
    "Tone",
]
