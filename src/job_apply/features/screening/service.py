"""Business logic for the ``screening`` slice (M3, issue #34).

The :class:`ScreeningService` is the integration seam between:

* the LLM (:class:`~job_apply.features.scoring.llm.LLMClient` from the
  scoring slice — no separate LLM client is needed because the
  existing Protocol is already the right shape),
* the screening repositories (questions + answers, both in-memory and
  SQL),
* the user / resume / vacancy slices it depends on through
  Protocol-typed lookups.

The service is collaborator-injected: tests build it with the
in-memory repositories and a fake LLM, the FastAPI dependency in
:mod:`api` builds it with the SQLAlchemy repositories and the
production LLM.

Idempotency contract
--------------------

:meth:`generate_answer` is **idempotent**: a repeat call for the same
``(question_id, user_id)`` pair updates the existing row rather than
inserting a second one. The repository's
:meth:`update` method is the only mutation path the service uses to
rewrite a stored answer, and the ``(question_id, user_id)`` unique
constraint on the table is the safety net if two requests race.

Resume lookup
-------------

The service picks the **latest** resume (newest ``created_at``) for
the user and uses its :attr:`plain_text` to build the prompt. When
the user has no resumes yet the prompt falls back to an empty resume
string so the LLM still produces a (much shorter) answer.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from typing import Any, Protocol, runtime_checkable

from job_apply.features.resumes.models import Resume
from job_apply.features.scoring.llm import LLMClient
from job_apply.features.screening.models import (
    ScreeningQuestion,
    ScreeningQuestionAnswer,
)
from job_apply.features.screening.prompts import (
    SCREENING_ANSWER_PROMPT_VERSION,
    build_screening_answer_prompt,
)
from job_apply.features.screening.repository import (
    ScreeningAnswerRepository,
    ScreeningQuestionRepository,
)
from job_apply.features.sources.models import Vacancy
from job_apply.features.users.models import User
from job_apply.shared.errors import NotFoundError

# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class ScreeningServiceError(Exception):
    """Base error for the screening service.

    Caught by the API layer and translated into 4xx / 5xx responses.
    Specific subclasses (``ScreeningQuestionNotFoundError``,
    ``ScreeningVacancyNotFoundError``) keep the boundary explicit.
    """

    code: str = "screening_error"


class ScreeningQuestionNotFoundError(ScreeningServiceError, NotFoundError):
    """The requested screening question does not exist."""

    code: str = "screening_question_not_found"


class ScreeningVacancyNotFoundError(ScreeningServiceError, NotFoundError):
    """The vacancy the service is being asked to operate on does not exist."""

    code: str = "vacancy_not_found"


# ---------------------------------------------------------------------------
# Cross-slice Protocol types
# ---------------------------------------------------------------------------


@runtime_checkable
class _UserLookup(Protocol):
    def get_by_id(self, user_id: uuid.UUID) -> User | None: ...


@runtime_checkable
class _VacancyLookup(Protocol):
    def get_by_id(self, vacancy_id: uuid.UUID) -> Vacancy | None: ...


@runtime_checkable
class _ResumeLookup(Protocol):
    def list_for_user(self, user_id: uuid.UUID) -> Sequence[Resume]: ...


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------


class ScreeningService:
    """Orchestrate the screening-question answer flow.

    The service is the only place in the slice that knows about the
    LLM, the user / resume / vacancy slices, and the persistence
    repositories. Tests inject in-memory fakes for every dependency;
    production wiring in :mod:`api` injects the SQLAlchemy-backed
    implementations.
    """

    def __init__(
        self,
        *,
        llm: LLMClient,
        question_repo: ScreeningQuestionRepository,
        answer_repo: ScreeningAnswerRepository,
        user_repo: _UserLookup,
        resume_repo: _ResumeLookup,
        vacancy_repo: _VacancyLookup,
        prompt_version: str = SCREENING_ANSWER_PROMPT_VERSION,
    ) -> None:
        self._llm = llm
        self._question_repo = question_repo
        self._answer_repo = answer_repo
        self._user_repo = user_repo
        self._resume_repo = resume_repo
        self._vacancy_repo = vacancy_repo
        self._prompt_version = prompt_version

    # ------------------------------------------------------------------
    # Public properties (test seams)
    # ------------------------------------------------------------------

    @property
    def llm(self) -> LLMClient:
        """Return the injected LLM client (read-only)."""
        return self._llm

    @property
    def question_repo(self) -> ScreeningQuestionRepository:
        """Return the injected question repository (read-only)."""
        return self._question_repo

    @property
    def answer_repo(self) -> ScreeningAnswerRepository:
        """Return the injected answer repository (read-only)."""
        return self._answer_repo

    # ------------------------------------------------------------------
    # Writers
    # ------------------------------------------------------------------

    def add_questions_to_vacancy(
        self,
        vacancy_id: uuid.UUID,
        questions: list[str],
    ) -> list[ScreeningQuestion]:
        """Persist a list of question texts for ``vacancy_id``.

        Each text becomes a row with a sequential ``question_order``
        index starting at 0. Empty input is a no-op and returns an
        empty list. The caller (HTTP layer) is expected to have
        validated that ``vacancy_id`` exists; the service does not
        re-check the vacancy here.
        """
        created: list[ScreeningQuestion] = []
        for index, text in enumerate(questions):
            row = ScreeningQuestion(
                vacancy_id=vacancy_id,
                question_text=text,
                question_order=index,
            )
            created.append(self._question_repo.create(row))
        return created

    async def generate_answer(
        self,
        question_id: uuid.UUID,
        user_id: uuid.UUID,
    ) -> ScreeningQuestionAnswer:
        """Generate (or refresh) the user's answer for ``question_id``.

        The method is **idempotent**: if an answer already exists for
        the ``(question_id, user_id)`` pair it is updated in place
        rather than inserted as a duplicate. The unique constraint on
        the table is the safety net for racing requests.
        """
        question = self._question_repo.get_by_id(question_id)
        if question is None:
            raise ScreeningQuestionNotFoundError(f"screening question {question_id} not found")

        # The vacancy, resume, and user are best-effort lookups: if any
        # of them is missing the prompt just omits that block. The only
        # hard requirement is the question — a missing question is the
        # one thing we cannot work around.
        vacancy = self._vacancy_repo.get_by_id(question.vacancy_id)
        vacancy_context = self._vacancy_context(vacancy) if vacancy is not None else None
        resume_text = self._latest_resume_plain_text(user_id)

        prompt = build_screening_answer_prompt(
            question=question.question_text,
            resume_text=resume_text,
            vacancy_context=vacancy_context,
        )
        answer_text = await self._llm.complete(prompt)
        # ``llm.complete`` returns ``str``; the cast through ``str()``
        # keeps a non-LLM stub that returns a ``str`` subclass happy.
        answer_text_str = str(answer_text).strip()

        existing = self._find_existing_answer(question_id, user_id)
        if existing is not None:
            return self._answer_repo.update(
                existing.id,
                answer_text=answer_text_str,
                prompt_version=self._prompt_version,
                model_used=self._model_name(),
            )

        row = ScreeningQuestionAnswer(
            question_id=question_id,
            user_id=user_id,
            answer_text=answer_text_str,
            prompt_version=self._prompt_version,
            model_used=self._model_name(),
        )
        return self._answer_repo.create(row)

    async def generate_answers_for_vacancy(
        self,
        vacancy_id: uuid.UUID,
        user_id: uuid.UUID,
    ) -> list[ScreeningQuestionAnswer]:
        """Generate an answer for every question attached to ``vacancy_id``.

        Questions are processed in ``question_order`` order. The
        method is best-effort: if any individual answer fails it
        bubbles up unhandled and the caller can decide whether to
        retry the remaining questions.
        """
        questions = list(self._question_repo.list_by_vacancy(vacancy_id))
        answers: list[ScreeningQuestionAnswer] = []
        for question in questions:
            answers.append(await self.generate_answer(question.id, user_id))
        return answers

    # ------------------------------------------------------------------
    # Readers
    # ------------------------------------------------------------------

    def list_user_answers(
        self,
        user_id: uuid.UUID,
        *,
        vacancy_id: uuid.UUID | None = None,
    ) -> list[ScreeningQuestionAnswer]:
        """Return every answer owned by ``user_id``.

        ``vacancy_id`` is an optional secondary filter — when set the
        listing is narrowed to answers whose question belongs to that
        vacancy. The filter is pushed down to the repository so the
        SQL implementation does not pay an N+1.
        """
        return list(self._answer_repo.list_by_user(user_id, vacancy_id=vacancy_id))

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _vacancy_context(self, vacancy: Vacancy) -> str:
        """Build the optional vacancy-context block for the prompt.

        The block is a short, human-readable summary: title,
        employer, location, and the head of the description. Long
        descriptions are truncated to keep the prompt compact.
        """
        parts: list[str] = []
        if vacancy.title:
            parts.append(f"Title: {vacancy.title}")
        if vacancy.employer_name:
            parts.append(f"Employer: {vacancy.employer_name}")
        if vacancy.location:
            parts.append(f"Location: {vacancy.location}")
        if vacancy.description:
            snippet = vacancy.description.strip().replace("\n", " ")
            if len(snippet) > 240:
                snippet = snippet[:237].rstrip() + "..."
            parts.append(f"Description: {snippet}")
        return "\n".join(parts)

    def _latest_resume_plain_text(self, user_id: uuid.UUID) -> str:
        """Return the newest resume's ``plain_text`` for ``user_id``.

        An empty string is returned when the user has no resumes — the
        prompt builder treats that as "(no resume provided)".
        """
        resumes = list(self._resume_repo.list_for_user(user_id))
        if not resumes:
            return ""
        resumes.sort(
            key=lambda r: (r.created_at is None, r.created_at, r.id),
        )
        return resumes[-1].plain_text or ""

    def _find_existing_answer(
        self,
        question_id: uuid.UUID,
        user_id: uuid.UUID,
    ) -> ScreeningQuestionAnswer | None:
        """Return the user's existing answer for ``question_id`` if any."""
        rows = self._answer_repo.list_by_user_question(user_id, question_id)
        return rows[0] if rows else None

    def _model_name(self) -> str | None:
        """Return a model name from the LLM client if it exposes one.

        The :class:`LLMClient` Protocol does not require a
        ``model_name`` attribute, so the helper uses ``getattr`` with a
        default. Real clients (``HttpLLMClient``) carry one; the
        in-memory test fake does not — that's fine, the column is
        nullable.
        """
        candidate: Any = getattr(self._llm, "model", None)
        if candidate is None:
            return None
        return str(candidate)


__all__ = [
    "ScreeningQuestionNotFoundError",
    "ScreeningService",
    "ScreeningServiceError",
    "ScreeningVacancyNotFoundError",
]
