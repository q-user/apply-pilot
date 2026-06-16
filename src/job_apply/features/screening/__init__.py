"""Screening-question answer vertical slice (M3, issue #34).

This slice owns the persistence + business logic for the
LLM-suggested answers to screening questions a job application asks
the candidate. The slice is small but spans the full stack:

* :class:`ScreeningQuestion` and :class:`ScreeningQuestionAnswer`
  — the ORM models.
* :class:`ScreeningQuestionRepository` and
  :class:`ScreeningAnswerRepository` — the storage Protocol
  contracts and their in-memory + SQL implementations.
* :data:`SCREENING_ANSWER_PROMPT_V1` and
  :func:`build_screening_answer_prompt` — the canonical prompt
  template.
* :class:`ScreeningService` — the use-case orchestrator that ties
  the LLM, the repositories, and the user / resume / vacancy slices
  together.
* :class:`ScreeningQuestionExtractor` — the Protocol the
  ``SourceService`` depends on for the M2 capture flow (issue #26).
  :class:`HhScreeningQuestionExtractor` is the default
  implementation.
* :data:`router` — the FastAPI router (mounted from
  :mod:`job_apply.app`).

Public surface
--------------

The slice exposes the ORM models, both repository protocols (and
their in-memory + SQL implementations), the canonical prompt
version, the service, the screening-question extractor, and the
router. The HTTP layer is wired into the FastAPI application by
:mod:`job_apply.app`; the in-process callers (Telegram actions,
daily digest, etc.) depend on the service directly.
"""

from __future__ import annotations

from job_apply.features.screening.extractor import (
    HhScreeningQuestionExtractor,
    ScreeningQuestionExtractor,
)
from job_apply.features.screening.models import (
    ScreeningQuestion,
    ScreeningQuestionAnswer,
)
from job_apply.features.screening.prompts import (
    SCREENING_ANSWER_PROMPT_V1,
    SCREENING_ANSWER_PROMPT_VERSION,
    build_screening_answer_prompt,
)
from job_apply.features.screening.repository import (
    InMemoryScreeningAnswerRepository,
    InMemoryScreeningQuestionRepository,
    ScreeningAnswerRepository,
    ScreeningQuestionRepository,
    SqlScreeningAnswerRepository,
    SqlScreeningQuestionRepository,
)
from job_apply.features.screening.schemas import (
    AddQuestionsRequest,
    ScreeningQuestionAnswerRead,
    ScreeningQuestionRead,
)
from job_apply.features.screening.service import (
    ScreeningQuestionNotFoundError,
    ScreeningService,
    ScreeningServiceError,
    ScreeningVacancyNotFoundError,
)

__all__ = [
    "AddQuestionsRequest",
    "HhScreeningQuestionExtractor",
    "InMemoryScreeningAnswerRepository",
    "InMemoryScreeningQuestionRepository",
    "SCREENING_ANSWER_PROMPT_V1",
    "SCREENING_ANSWER_PROMPT_VERSION",
    "ScreeningAnswerRepository",
    "ScreeningQuestion",
    "ScreeningQuestionAnswer",
    "ScreeningQuestionAnswerRead",
    "ScreeningQuestionExtractor",
    "ScreeningQuestionNotFoundError",
    "ScreeningQuestionRead",
    "ScreeningQuestionRepository",
    "ScreeningService",
    "ScreeningServiceError",
    "ScreeningVacancyNotFoundError",
    "SqlScreeningAnswerRepository",
    "SqlScreeningQuestionRepository",
    "build_screening_answer_prompt",
]
