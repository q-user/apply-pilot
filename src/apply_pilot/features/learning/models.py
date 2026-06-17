"""ORM model + value object for the learning-signals slice (M8, issue #63).

This module exposes the two value objects the slice trades in:

* :class:`LearningSignalRow` — the SQLAlchemy ORM row backed by the
  ``learning_signals`` table.
* :class:`LearningSignal` — the immutable, framework-agnostic value
  object the service layer and API DTOs use.

The schema is described in the module-level docstring of
:class:`LearningSignalRow`; the value object's public surface is
described in its own docstring.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from sqlalchemy import DateTime, Float, Index, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from apply_pilot.db import Base
from apply_pilot.shared.types import GUID

# ``Float`` is the column type for ``score`` so future LLM scoring
# passes can use a finer-grained value without another migration.
# ``String(32)`` for ``signal_type`` comfortably fits the three
# discriminators (``rejection`` / ``dismissal`` / ``low_score``) and
# leaves headroom for the next couple of additions.


class LearningSignalRow(Base):
    """A single user-action signal that the future prompt tuning pipeline reads.

    The ``learning_signals`` table captures one row per user action
    that feeds back into the future prompt-tuning pipeline. The
    current producer is the ``/reject`` Telegram action (M4, issue
    #38); future producers will include dismissals and "low-score"
    hits from the review queue.

    Each row stores:

    * ``user_id`` — the user that triggered the signal. Indexed for
      the per-user read path used by the ``GET /learning/signals``
      endpoint.
    * ``match_id``, ``vacancy_id``, ``search_profile_id`` — the join
      keys needed to look the original recommendation up after the
      fact.
    * ``rejection_reason`` — free-form text supplied by the user.
      May be NULL if the user rejected without a reason.
    * ``prompt_version`` — the active prompt version the row was
      scored with (NULL when the match had never been through the
      LLM pipeline).
    * ``score`` — the score the match carried at reject time,
      normalised to a float in ``[0.0, 100.0]``. NULL on a
      freshly-ingested match.
    * ``signal_type`` — narrow discriminator so future producers
      (e.g. dismissals) can share the table without conflicting on
      a single column.
    * ``created_at`` — server-side timestamp with timezone.

    Schema notes
    ------------

    * ``UNIQUE`` is intentionally not enforced on
      ``(user_id, match_id)`` — the slice wants the full history of
      reject / dismiss / low-score events for a single match, and a
      re-reject from the same user is valuable training data.
    * ``Index`` on ``(user_id, created_at desc)`` is the per-user
      read path used by the read endpoint.
    * ``Index`` on ``(prompt_version, created_at desc)`` is the
      per-prompt read path the future prompt-tuning pipeline will
      hit.
    """

    __tablename__ = "learning_signals"
    __table_args__ = (
        # Per-user read path: ``list_for_user`` orders by created_at
        # descending.
        Index(
            "ix_learning_signals_user_created",
            "user_id",
            "created_at",
        ),
        # Per-prompt read path: ``list_for_prompt`` orders by created_at
        # descending.
        Index(
            "ix_learning_signals_prompt_created",
            "prompt_version",
            "created_at",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=uuid.uuid4)

    user_id: Mapped[uuid.UUID] = mapped_column(
        GUID(),
        nullable=False,
        index=True,
    )
    match_id: Mapped[uuid.UUID] = mapped_column(GUID(), nullable=False)
    vacancy_id: Mapped[uuid.UUID] = mapped_column(GUID(), nullable=False)
    search_profile_id: Mapped[uuid.UUID] = mapped_column(GUID(), nullable=False)

    rejection_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    prompt_version: Mapped[str | None] = mapped_column(String(50), nullable=True)
    score: Mapped[float | None] = mapped_column(Float, nullable=True)
    signal_type: Mapped[str] = mapped_column(String(32), nullable=False)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return (
            f"LearningSignalRow(id={self.id!s}, user_id={self.user_id!s}, "
            f"signal_type={self.signal_type!r})"
        )


@dataclass(frozen=True)
class LearningSignal:
    """An immutable description of a single user-action signal row.

    Public surface (kept stable for downstream consumers, especially
    the future prompt-tuning pipeline):

    * ``id`` — server-generated UUID, stable across re-reads.
    * ``user_id`` — the user that triggered the signal.
    * ``match_id``, ``vacancy_id``, ``search_profile_id`` — the join
      keys for looking the original recommendation up after the fact.
    * ``rejection_reason`` — free-form text supplied by the user; may
      be ``None`` when the user did not supply a reason.
    * ``prompt_version`` — the active prompt version the match was
      scored with; ``None`` on a freshly-ingested match.
    * ``score`` — the score the match carried at signal time, in
      ``[0.0, 100.0]``; ``None`` on a freshly-ingested match.
    * ``signal_type`` — narrow discriminator; one of ``"rejection"``,
      ``"dismissal"``, ``"low_score"``.
    * ``created_at`` — UTC timestamp with timezone.
    """

    id: uuid.UUID
    user_id: uuid.UUID
    match_id: uuid.UUID
    vacancy_id: uuid.UUID
    search_profile_id: uuid.UUID
    rejection_reason: str | None
    prompt_version: str | None
    score: float | None
    signal_type: Literal["rejection", "dismissal", "low_score"]
    created_at: datetime


__all__ = ["LearningSignal", "LearningSignalRow"]
