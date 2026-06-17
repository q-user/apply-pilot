"""Cover letter style preferences — business logic.

The service owns:

* The "one style per user" contract.
* The default-value policy (a default style is constructed in memory
  and never persisted by :meth:`CoverLetterStyleService.get_or_default`).
* The mapping between the ORM row and the public DTO.

The repository does the actual JSON encoding of the list columns; the
service deals only in ``list[str]``.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import cast

from apply_pilot.features.cover_letter_style.models import CoverLetterStyle
from apply_pilot.features.cover_letter_style.repository import CoverLetterStyleRepository
from apply_pilot.features.cover_letter_style.schemas import (
    ALLOWED_LENGTHS,
    ALLOWED_TONES,
    CoverLetterStyleRead,
    CoverLetterStyleUpdate,
    Length,
    Tone,
)

# Default values applied when the user has no style yet. Used by
# ``get_or_default`` and by ``upsert`` to fill in fields the caller
# omitted from the update payload.
DEFAULT_TONE = "professional"
DEFAULT_LENGTH = "medium"
DEFAULT_FOCUS_AREAS: list[str] = []
DEFAULT_AVOID_PHRASES: list[str] = []


def _style_to_dto(style: CoverLetterStyle) -> CoverLetterStyleRead:
    """Map an ORM row to the public DTO.

    Assumes the repository already decoded the JSON list columns; this
    function is the very last step before the data leaves the slice.
    """
    return CoverLetterStyleRead(
        id=style.id,
        user_id=style.user_id,
        tone=cast(Tone, style.tone),
        length=cast(Length, style.length),
        focus_areas=list(style.focus_areas or []),
        avoid_phrases=list(style.avoid_phrases or []),
        extra_instructions=style.extra_instructions,
        created_at=style.created_at,
        updated_at=style.updated_at,
    )


def _build_default_style(user_id: uuid.UUID) -> CoverLetterStyle:
    """Construct a default style in memory (not persisted)."""
    now = datetime.now(UTC)
    style = CoverLetterStyle(
        id=uuid.uuid4(),
        user_id=user_id,
        tone=DEFAULT_TONE,
        length=DEFAULT_LENGTH,
        focus_areas=list(DEFAULT_FOCUS_AREAS),
        avoid_phrases=list(DEFAULT_AVOID_PHRASES),
        extra_instructions=None,
        created_at=now,
    )
    return style


class CoverLetterStyleService:
    """CRUD for per-user cover-letter style preferences.

    The service surface is intentionally tiny — one style per user, no
    pagination, no listing. The API mirrors this with three endpoints
    (``GET``, ``PUT``, ``DELETE``) keyed by the authenticated user.
    """

    def __init__(self, repository: CoverLetterStyleRepository) -> None:
        self._repo = repository

    @property
    def repo(self) -> CoverLetterStyleRepository:
        """Expose the repository for tests that need to assert state."""
        return self._repo

    # ------------------------------------------------------------------
    # Read
    # ------------------------------------------------------------------

    def get_or_default(self, user_id: uuid.UUID) -> CoverLetterStyleRead:
        """Return the user's persisted style, or a default if none exists.

        The default is constructed in memory and **never** persisted —
        the next ``upsert`` is what materialises a row in the database.
        This matches the documented M3 behaviour: callers always see a
        style, even for users who have never set one.
        """
        existing = self._repo.get_by_user(user_id)
        if existing is not None:
            return _style_to_dto(existing)
        return _style_to_dto(_build_default_style(user_id))

    # ------------------------------------------------------------------
    # Write
    # ------------------------------------------------------------------

    def upsert(
        self,
        user_id: uuid.UUID,
        payload: CoverLetterStyleUpdate,
    ) -> CoverLetterStyleRead:
        """Create the style for ``user_id`` if absent, else update it.

        Every field is optional in the payload; omitted fields keep
        their existing value (or fall back to the documented default
        for a fresh insert).
        """
        existing = self._repo.get_by_user(user_id)
        if existing is None:
            return self._create_from_payload(user_id, payload)
        return self._update_from_payload(existing, payload)

    def _create_from_payload(
        self,
        user_id: uuid.UUID,
        payload: CoverLetterStyleUpdate,
    ) -> CoverLetterStyleRead:
        """Materialise a new style row from a (possibly minimal) payload."""
        style = CoverLetterStyle(
            user_id=user_id,
            tone=payload.tone or DEFAULT_TONE,
            length=payload.length or DEFAULT_LENGTH,
            focus_areas=list(payload.focus_areas)
            if payload.focus_areas is not None
            else list(DEFAULT_FOCUS_AREAS),
            avoid_phrases=list(payload.avoid_phrases)
            if payload.avoid_phrases is not None
            else list(DEFAULT_AVOID_PHRASES),
            extra_instructions=payload.extra_instructions,
        )
        self._validate_tone(style.tone)
        self._validate_length(style.length)
        created = self._repo.create(style)
        return _style_to_dto(created)

    def _update_from_payload(
        self,
        existing: CoverLetterStyle,
        payload: CoverLetterStyleUpdate,
    ) -> CoverLetterStyleRead:
        """Mutate the existing row with whatever the payload supplied."""
        if payload.tone is not None:
            self._validate_tone(payload.tone)
            existing.tone = payload.tone
        if payload.length is not None:
            self._validate_length(payload.length)
            existing.length = payload.length
        if payload.focus_areas is not None:
            existing.focus_areas = list(payload.focus_areas)  # ty: ignore[invalid-assignment]
        if payload.avoid_phrases is not None:
            existing.avoid_phrases = list(payload.avoid_phrases)  # ty: ignore[invalid-assignment]
        if "extra_instructions" in payload.model_fields_set:
            existing.extra_instructions = payload.extra_instructions
        updated = self._repo.update(existing)
        return _style_to_dto(updated)

    # ------------------------------------------------------------------
    # Delete
    # ------------------------------------------------------------------

    def delete(self, user_id: uuid.UUID) -> bool:
        """Remove the user's style.

        Returns ``True`` if a row was deleted, ``False`` if no style
        existed (idempotent semantics for the HTTP layer).
        """
        return self._repo.delete_by_user(user_id)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _validate_tone(tone: str) -> None:
        if tone not in ALLOWED_TONES:
            from apply_pilot.shared.errors import ValidationError

            raise ValidationError(f"tone must be one of {list(ALLOWED_TONES)}; got {tone!r}")

    @staticmethod
    def _validate_length(length: str) -> None:
        if length not in ALLOWED_LENGTHS:
            from apply_pilot.shared.errors import ValidationError

            raise ValidationError(f"length must be one of {list(ALLOWED_LENGTHS)}; got {length!r}")


__all__ = [
    "CoverLetterStyleService",
    "DEFAULT_AVOID_PHRASES",
    "DEFAULT_FOCUS_AREAS",
    "DEFAULT_LENGTH",
    "DEFAULT_TONE",
]
