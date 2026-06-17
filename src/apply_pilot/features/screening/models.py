"""ORM models for the ``screening`` slice (M3, issue #34).

The slice owns two tables:

* :class:`ScreeningQuestion` — a question a job application asks the
  candidate (e.g. "Why do you want to work here?"). Questions are
  attached to a :class:`~apply_pilot.features.sources.models.Vacancy`
  and are stable across the lifetime of that vacancy.
* :class:`ScreeningQuestionAnswer` — the LLM-suggested answer for a
  :class:`ScreeningQuestion` by a specific user. One row per
  ``(question_id, user_id)`` pair; a regeneration updates the same
  row rather than inserting a second one (idempotency is enforced
  by the service in
  :mod:`apply_pilot.features.screening.service`).

Schema
------

``screening_questions``:

* ``id``              — UUID primary key.
* ``vacancy_id``      — FK to ``vacancies.id``. Cascades on delete so
                        a removed vacancy leaves no orphan questions.
* ``question_text``   — the question, free text.
* ``question_order``  — 0-based index the candidate sees them in.
                        The default is 0; the service assigns
                        sequential values when bulk-loading.
* ``created_at``      — server-side timestamp.

``screening_question_answers``:

* ``id``              — UUID primary key.
* ``question_id``     — FK to ``screening_questions.id``. Cascades
                        on delete so removing a question drops its
                        answers.
* ``user_id``         — FK to ``users.id``. Cascades on delete so
                        removing a user drops their answers.
* ``answer_text``     — the LLM-suggested answer body.
* ``prompt_version``  — ``<name>@<semver>`` stamp mirroring the
                        :class:`PromptVersion` value object. The
                        service fills this with
                        :data:`~apply_pilot.features.screening.prompts.SCREENING_ANSWER_PROMPT_VERSION`.
* ``model_used``      — the LLM model name that produced the text.
                        ``NULL`` when the client does not expose a
                        model attribute.
* ``created_at`` / ``updated_at`` — server-side timestamps.

Indexes
-------

* ``ix_screening_questions_vacancy`` (single column on ``vacancy_id``)
  backs the "all questions for a vacancy" listing.
* ``ix_screening_question_answers_user`` (single column on
  ``user_id``) backs the per-user listing that powers the
  ``GET /screening/answers`` endpoint.
* ``uq_screening_question_answers_question_user`` — the
  ``(question_id, user_id)`` unique constraint that the service
  relies on for idempotent ``generate_answer`` calls.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from apply_pilot.db import Base
from apply_pilot.shared.types import GUID


class ScreeningQuestion(Base):
    """A single screening question attached to a vacancy.

    See module docstring for field semantics.
    """

    __tablename__ = "screening_questions"
    __table_args__ = (Index("ix_screening_questions_vacancy", "vacancy_id"),)

    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=uuid.uuid4)
    vacancy_id: Mapped[uuid.UUID] = mapped_column(
        GUID(),
        ForeignKey("vacancies.id", ondelete="CASCADE"),
        nullable=False,
    )
    question_text: Mapped[str] = mapped_column(Text, nullable=False)
    question_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return (
            f"ScreeningQuestion(id={self.id!s}, vacancy_id={self.vacancy_id!s}, "
            f"order={self.question_order})"
        )


class ScreeningQuestionAnswer(Base):
    """An LLM-suggested answer for one user on one question.

    The ``(question_id, user_id)`` pair is unique: a repeat call to
    :meth:`~apply_pilot.features.screening.service.ScreeningService.generate_answer`
    updates the existing row rather than inserting a second one.
    """

    __tablename__ = "screening_question_answers"
    __table_args__ = (
        Index("ix_screening_question_answers_user", "user_id"),
        UniqueConstraint(
            "question_id",
            "user_id",
            name="uq_screening_question_answers_question_user",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=uuid.uuid4)
    question_id: Mapped[uuid.UUID] = mapped_column(
        GUID(),
        ForeignKey("screening_questions.id", ondelete="CASCADE"),
        nullable=False,
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        GUID(),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    answer_text: Mapped[str] = mapped_column(Text, nullable=False)
    prompt_version: Mapped[str] = mapped_column(String(50), nullable=False)
    model_used: Mapped[str | None] = mapped_column(String(100), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), onupdate=func.now(), nullable=True
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return (
            f"ScreeningQuestionAnswer(id={self.id!s}, question_id={self.question_id!s}, "
            f"user_id={self.user_id!s}, version={self.prompt_version!r})"
        )


__all__ = [
    "ScreeningQuestion",
    "ScreeningQuestionAnswer",
]
