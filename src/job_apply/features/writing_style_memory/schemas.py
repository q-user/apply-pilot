"""Pydantic DTOs for the ``writing_style_memory`` slice.

The HTTP layer exchanges a single ``StyleMemoryRead`` shape: the
``user_id`` and the precomputed ``aggregated_summary`` string. The
slice is intentionally read-only on the public API; the ingestion
pipeline is wired into the ``/accept`` Telegram action, not the
HTTP surface (issue #66).
"""

from __future__ import annotations

import uuid

from pydantic import BaseModel, ConfigDict


class StyleMemoryRead(BaseModel):
    """Output shape for ``GET /writing-style-memory/me``.

    ``aggregated_summary`` is the precomputed concatenation of the
    user's most recent style-memory entries. ``None`` means the user
    has not accepted any cover letter yet.
    """

    model_config = ConfigDict(extra="forbid", frozen=False)

    user_id: uuid.UUID
    aggregated_summary: str | None = None


__all__ = ["StyleMemoryRead"]
