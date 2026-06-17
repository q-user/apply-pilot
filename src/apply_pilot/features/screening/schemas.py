"""Pydantic DTOs for the ``screening`` slice (M3, issue #34).

The HTTP / service boundary exchanges :class:`ScreeningQuestionRead`
and :class:`ScreeningQuestionAnswerRead` DTOs. The repository is the
only layer that knows the ORM row; the service maps ORM → DTO and is
the only place that decides which fields are public.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class ScreeningQuestionRead(BaseModel):
    """Public view of a single screening question attached to a vacancy.

    The DTO mirrors the ORM row but is intentionally a separate type
    so that the service can choose which fields to expose and so that
    adding internal columns to the model does not accidentally leak
    them to the HTTP layer.
    """

    model_config = ConfigDict(extra="forbid", frozen=False)

    id: uuid.UUID
    vacancy_id: uuid.UUID
    question_text: str
    question_order: int
    created_at: datetime


class ScreeningQuestionAnswerRead(BaseModel):
    """Public view of a single screening-question answer.

    The DTO mirrors the ORM row but is intentionally a separate type
    so that the service can choose which fields to expose and so that
    adding internal columns to the model does not accidentally leak
    them to the HTTP layer.
    """

    model_config = ConfigDict(extra="forbid", frozen=False)

    id: uuid.UUID
    question_id: uuid.UUID
    user_id: uuid.UUID
    answer_text: str
    prompt_version: str
    model_used: str | None = None
    created_at: datetime
    updated_at: datetime | None = None


class AddQuestionsRequest(BaseModel):
    """Body of ``POST /screening/questions/{vacancy_id}``.

    ``questions`` is the ordered list of question texts the candidate
    needs to answer. Empty strings are rejected by the ``min_length=1``
    constraint on each item.
    """

    model_config = ConfigDict(extra="forbid", frozen=False)

    questions: list[str] = Field(min_length=1)


__all__ = [
    "AddQuestionsRequest",
    "ScreeningQuestionAnswerRead",
    "ScreeningQuestionRead",
]
