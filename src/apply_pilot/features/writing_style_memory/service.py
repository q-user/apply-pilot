"""Service layer for the ``writing_style_memory`` slice.

The service is the bridge between the ``/accept`` Telegram action and
the persistence gateway: it ingests an accepted cover letter,
derives a deterministic ``style_summary`` from the letter's text, and
exposes a read-side that returns the user's aggregated summary for
the API.

The summary algorithm is intentionally simple — no LLM, no external
service. See :mod:`apply_pilot.features.writing_style_memory.summariser`
for the output format. LLM-based summarisation is a follow-up; the
service can swap the helper without touching any other layer.
"""

from __future__ import annotations

import logging
import uuid

from apply_pilot.features.writing_style_memory.models import (
    StyleMemory,
    StyleMemoryEntry,
)
from apply_pilot.features.writing_style_memory.repository import (
    DEFAULT_AGGREGATED_LIMIT,
    StyleMemoryRepository,
)
from apply_pilot.features.writing_style_memory.summariser import summarise_letter

_LOGGER = logging.getLogger("apply_pilot.features.writing_style_memory.service")


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------


class StyleMemoryService:
    """Business logic for the writing-style memory slice.

    The service is intentionally small — it owns the summary
    derivation and the empty-input policy, and delegates persistence
    to the injected :class:`StyleMemoryRepository`.
    """

    def __init__(self, *, repository: StyleMemoryRepository) -> None:
        self._repository = repository

    @property
    def repository(self) -> StyleMemoryRepository:
        """Expose the repository for tests that need to assert state."""
        return self._repository

    # ------------------------------------------------------------------
    # Ingestion
    # ------------------------------------------------------------------

    def record_accepted_letter(
        self,
        *,
        user_id: uuid.UUID,
        cover_letter_id: uuid.UUID,
        letter_text: str,
    ) -> StyleMemoryEntry | None:
        """Record an accepted cover letter into the user's style memory.

        ``letter_text`` is the body of the accepted cover letter.
        A blank or whitespace-only letter is treated as a no-op: the
        method returns ``None`` and no row is written. The caller can
        safely call this method without pre-validating the input.

        Returns the persisted :class:`StyleMemoryEntry` on success.
        """
        body = (letter_text or "").strip()
        if not body:
            return None

        summary = summarise_letter(body)
        if not summary:
            # ``summarise_letter`` returns "" only for blank input; the
            # guards above already catch that. Defensive: do not write
            # an entry without a summary.
            return None

        return self._repository.record(
            user_id=user_id,
            cover_letter_id=cover_letter_id,
            letter_text=body,
            style_summary=summary,
        )

    # ------------------------------------------------------------------
    # Read
    # ------------------------------------------------------------------

    def get_aggregated_summary(
        self,
        user_id: uuid.UUID,
        *,
        limit: int = DEFAULT_AGGREGATED_LIMIT,
    ) -> str | None:
        """Return the user's aggregated style summary, or ``None``.

        Delegates to the repository's :meth:`get_aggregated`. Pulled
        onto the service so the API layer does not need to import the
        repository directly.
        """
        return self._repository.get_aggregated(user_id, limit=limit)

    def get_memory(self, user_id: uuid.UUID) -> StyleMemory:
        """Return the full :class:`StyleMemory` aggregate for ``user_id``."""
        entries = list(self._repository.list_for_user(user_id))
        return StyleMemory(
            user_id=user_id,
            entries=entries,
            aggregated_summary=self._repository.get_aggregated(user_id),
        )


__all__ = [
    "StyleMemoryService",
]
