"""Resumes DTOs.

Two layers of DTOs live here:

* :class:`UploadedFile` — the transport-shaped value object the API hands
  to the service after multipart parsing. It deliberately does **not**
  depend on Pydantic so the service layer can stay testable without a
  schema framework on the data path.
* The Pydantic :class:`ResumeDTO` and :class:`ResumeListResponse` are
  the public shape returned to HTTP callers.

We do not use :class:`IdentifiedSchema` / :class:`TimestampedSchema` from
``apply_pilot.shared.schemas`` because those mixins assume integer primary
keys; resumes are keyed by UUID.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

# ---------------------------------------------------------------------------
# Transport value object (service-layer input)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class UploadedFile:
    """In-memory representation of a freshly uploaded resume file.

    The FastAPI handler builds this from a parsed ``multipart/form-data``
    request and passes it to :meth:`ResumesService.upload_resume`.
    The dataclass is frozen so the service can rely on its fields being
    immutable while the validation pass runs.
    """

    filename: str
    content_type: str
    size: int
    content: bytes


# ---------------------------------------------------------------------------
# Public DTOs (HTTP responses)
# ---------------------------------------------------------------------------


class ResumeDTO(BaseModel):
    """Public representation of a stored resume."""

    model_config = ConfigDict(extra="forbid", frozen=False)

    id: uuid.UUID
    user_id: uuid.UUID
    filename: str
    content_type: str
    size: int = Field(ge=0, description="Number of bytes stored for this resume.")
    raw_text: str = Field(
        description="Original text exactly as received, before any future normalisation."
    )
    plain_text: str = Field(
        description="Extracted plain text. Equal to ``raw_text`` for .txt/.md uploads."
    )
    created_at: datetime
    updated_at: datetime | None = None


class ResumeListResponse(BaseModel):
    """Response envelope for ``GET /resumes``."""

    model_config = ConfigDict(extra="forbid", frozen=False)

    items: list[ResumeDTO]


__all__ = ["ResumeDTO", "ResumeListResponse", "UploadedFile"]
