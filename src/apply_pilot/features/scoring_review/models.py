"""In-process value object for the scoring review slice (M8, issue #68).

A :class:`LowConfidenceMatch` is the snapshot of a :class:`VacancyMatch`
the admin queue surfaces for manual review. The dataclass is frozen
so the queue cannot accidentally be mutated downstream — the API layer
copies every field into the wire format and never hands out the raw
value object for in-place editing.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class LowConfidenceMatch:
    """Read-only view of a low-confidence match for the admin review queue.

    Attributes:
        match_id: Primary key of the underlying :class:`VacancyMatch` row.
        vacancy_id: The vacancy the match points at.
        user_id: Owner of the search profile the match is tied to.
            Resolved through the ``search_profiles`` table so the admin
            can drill down without a second round-trip.
        search_profile_id: The profile that produced the match.
        score: The LLM-assigned score in ``[0, 100]`` (matches the
            ``vacancy_matches.score`` column).
        confidence: The LLM's self-reported certainty in ``[0.0, 1.0]``.
            ``None`` is impossible — :meth:`ScoringReviewQueue.list_low_confidence`
            filters such rows out — but the type stays ``float | None`` so
            the field round-trips through the ORM column unchanged.
        prompt_version: Identifier of the prompt that produced the score
            (matches ``vacancy_matches.prompt_version``). Useful for
            attributing a bad row to a specific prompt revision.
        explanation: The LLM's free-form explanation, exactly as stored
            on the match row. May be ``None`` for rows scored before
            explanations were captured.
        created_at: UTC timestamp the match was inserted. Used to break
            ties when two rows share a confidence value.
    """

    match_id: uuid.UUID
    vacancy_id: uuid.UUID
    user_id: uuid.UUID
    search_profile_id: uuid.UUID
    score: int | None
    confidence: float | None
    prompt_version: str | None
    explanation: str | None
    created_at: datetime


__all__ = ["LowConfidenceMatch"]
