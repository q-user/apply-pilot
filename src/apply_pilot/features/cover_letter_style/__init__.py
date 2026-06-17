"""Cover letter style preferences vertical slice.

Public surface
--------------

* :class:`CoverLetterStyle` — ORM model (one style per user).
* :class:`CoverLetterStyleService` — business logic.
* :class:`CoverLetterStyleRepository` — Protocol contract.
* :class:`InMemoryCoverLetterStyleRepository` — fake for tests.
* :class:`SqlCoverLetterStyleRepository` — production implementation.
* :class:`CoverLetterStyleRead` / :class:`CoverLetterStyleUpdate` —
  public DTOs.

Endpoints
---------

* ``GET /cover-letter-style`` — get my style (or default if none).
* ``PUT /cover-letter-style`` — upsert (full update with full style).
* ``DELETE /cover-letter-style`` — remove (idempotent).

The list-valued columns (``focus_areas``, ``avoid_phrases``) are stored
as JSON-encoded ``TEXT`` to keep the migration portable across sqlite
and PostgreSQL without an ``ARRAY`` type. The repository is the only
layer that knows about the encoding.
"""

from __future__ import annotations

from apply_pilot.features.cover_letter_style.models import CoverLetterStyle
from apply_pilot.features.cover_letter_style.repository import (
    CoverLetterStyleRepository,
    InMemoryCoverLetterStyleRepository,
    SqlCoverLetterStyleRepository,
)
from apply_pilot.features.cover_letter_style.schemas import (
    ALLOWED_LENGTHS,
    ALLOWED_TONES,
    CoverLetterStyleRead,
    CoverLetterStyleUpdate,
    Length,
    Tone,
)
from apply_pilot.features.cover_letter_style.service import (
    DEFAULT_AVOID_PHRASES,
    DEFAULT_FOCUS_AREAS,
    DEFAULT_LENGTH,
    DEFAULT_TONE,
    CoverLetterStyleService,
)

__all__ = [
    "ALLOWED_LENGTHS",
    "ALLOWED_TONES",
    "CoverLetterStyle",
    "CoverLetterStyleRead",
    "CoverLetterStyleRepository",
    "CoverLetterStyleService",
    "CoverLetterStyleUpdate",
    "DEFAULT_AVOID_PHRASES",
    "DEFAULT_FOCUS_AREAS",
    "DEFAULT_LENGTH",
    "DEFAULT_TONE",
    "InMemoryCoverLetterStyleRepository",
    "Length",
    "SqlCoverLetterStyleRepository",
    "Tone",
]
