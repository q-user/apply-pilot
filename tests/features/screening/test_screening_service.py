"""TDD tests for the :class:`ScreeningService` use cases (M3, issue #34).

The service is the integration seam between the LLM, the screening
repositories, and the user / resume / vacancy slices it depends on
through Protocol-typed repositories. The tests inject
:class:`InMemoryLLMClient` + in-memory fakes for every other
dependency — no ``Mock``, no real network.

Behaviour covered:

* :meth:`add_questions_to_vacancy` persists a list of question texts
  with stable ``question_order`` indices.
* :meth:`generate_answer` calls the LLM with the rendered prompt and
  stores the answer.
* :meth:`generate_answer` is **idempotent** — a second call updates
  the existing row rather than inserting a duplicate.
* :meth:`generate_answers_for_vacancy` answers every question for a
  vacancy in one call.
* :meth:`list_user_answers` filters by the calling user; ``vacancy_id``
  is an optional secondary filter.
* :meth:`generate_answer` raises when the question does not exist.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from typing import Protocol

import pytest

from job_apply.features.resumes.models import Resume
from job_apply.features.resumes.repository import ResumesRepository
from job_apply.features.scoring.llm import InMemoryLLMClient
from job_apply.features.screening.models import (
    ScreeningQuestion,
    ScreeningQuestionAnswer,
)
from job_apply.features.screening.repository import (
    InMemoryScreeningAnswerRepository,
    InMemoryScreeningQuestionRepository,
)
from job_apply.features.screening.service import (
    ScreeningService,
    ScreeningServiceError,
)
from job_apply.features.sources.models import Vacancy
from job_apply.features.sources.repository import (
    InMemoryVacancyRepository,
    SqlVacancyRepository,
)
from job_apply.features.users.models import User
from job_apply.features.users.repository import InMemoryUsersRepository

# ---------------------------------------------------------------------------
# Minimal Protocol types for the cross-slice fakes
# ---------------------------------------------------------------------------


class _UserRepoLike(Protocol):
    def get_by_id(self, user_id: uuid.UUID) -> User | None: ...


class _VacancyRepoLike(Protocol):
    def get_by_id(self, vacancy_id: uuid.UUID) -> Vacancy | None: ...


class _ResumeRepoLike(Protocol):
    def list_for_user(self, user_id: uuid.UUID) -> Sequence[Resume]: ...


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _FakeResumeRepository:
    """Mimic :class:`ResumesRepository` for the service tests."""

    def __init__(self, resume: Resume | None = None) -> None:
        self._resume = resume

    def list_for_user(self, user_id: uuid.UUID) -> Sequence[Resume]:
        return [self._resume] if self._resume is not None else []


def _build_resume(user_id: uuid.UUID, plain_text: str = "Senior Python developer") -> Resume:
    resume = Resume(
        user_id=user_id,
        filename="cv.pdf",
        content_type="application/pdf",
        size=len(plain_text),
        raw_text=plain_text,
        plain_text=plain_text,
    )
    resume.id = uuid.uuid4()
    return resume


_vacancy_counter = 0


def _build_vacancy(title: str = "Senior Python Developer") -> Vacancy:
    global _vacancy_counter
    _vacancy_counter += 1
    vacancy = Vacancy(
        source="hh",
        source_id=f"hh-screening-svc-{_vacancy_counter}",
        title=title,
        description="FastAPI + Postgres + AWS",
        raw_data={"id": f"hh-screening-svc-{_vacancy_counter}", "name": title},
    )
    vacancy.id = uuid.uuid4()
    return vacancy


def _build_user(email: str = "svc@example.com") -> User:
    user = User(id=uuid.uuid4(), email=email, hashed_password="x")
    return user


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def user() -> User:
    return _build_user()


@pytest.fixture
def vacancy() -> Vacancy:
    return _build_vacancy()


@pytest.fixture
def question_repo() -> InMemoryScreeningQuestionRepository:
    return InMemoryScreeningQuestionRepository()


@pytest.fixture
def answer_repo() -> InMemoryScreeningAnswerRepository:
    return InMemoryScreeningAnswerRepository()


@pytest.fixture
def user_repo(user: User) -> InMemoryUsersRepository:
    repo = InMemoryUsersRepository()
    repo.create(email=user.email, hashed_password=user.hashed_password, is_active=True)
    return repo


@pytest.fixture
def vacancy_repo(vacancy: Vacancy) -> InMemoryVacancyRepository:
    repo = InMemoryVacancyRepository()
    repo.upsert(vacancy)
    return repo


@pytest.fixture
def resume_repo(user: User) -> _FakeResumeRepository:
    return _FakeResumeRepository(resume=_build_resume(user.id))


@pytest.fixture
def llm() -> InMemoryLLMClient:
    return InMemoryLLMClient(
        responses={
            "*": "I am excited to apply because I love building reliable services.",
        }
    )


@pytest.fixture
def service(
    llm: InMemoryLLMClient,
    question_repo: InMemoryScreeningQuestionRepository,
    answer_repo: InMemoryScreeningAnswerRepository,
    user_repo: InMemoryUsersRepository,
    vacancy_repo: InMemoryVacancyRepository,
    resume_repo: _FakeResumeRepository,
) -> ScreeningService:
    # Wire the in-memory answer repo to the question repo so the
    # ``list_by_user(vacancy_id=...)`` filter can resolve the vacancy
    # in-memory. The SQL implementation does this via JOIN natively.
    answer_repo.attach_question_lookup(question_repo)
    return ScreeningService(
        llm=llm,
        question_repo=question_repo,
        answer_repo=answer_repo,
        user_repo=user_repo,  # type: ignore[arg-type]
        resume_repo=resume_repo,  # type: ignore[arg-type]
        vacancy_repo=vacancy_repo,  # type: ignore[arg-type]
    )


# ---------------------------------------------------------------------------
# add_questions_to_vacancy
# ---------------------------------------------------------------------------


class TestAddQuestionsToVacancy:
    def test_creates_one_row_per_question_with_sequential_order(
        self,
        service: ScreeningService,
        vacancy: Vacancy,
    ) -> None:
        """Each text becomes a row with a sequential ``question_order``."""
        texts = ["Why us?", "Years of experience?", "Notice period?"]
        created = service.add_questions_to_vacancy(vacancy.id, texts)
        assert [q.question_text for q in created] == texts
        assert [q.question_order for q in created] == [0, 1, 2]
        assert all(q.vacancy_id == vacancy.id for q in created)

    def test_returns_empty_list_for_empty_input(
        self,
        service: ScreeningService,
        vacancy: Vacancy,
    ) -> None:
        """An empty input is a no-op and returns an empty list."""
        assert service.add_questions_to_vacancy(vacancy.id, []) == []


# ---------------------------------------------------------------------------
# generate_answer
# ---------------------------------------------------------------------------


class TestGenerateAnswer:
    @pytest.mark.asyncio
    async def test_calls_llm_and_persists_answer(
        self,
        service: ScreeningService,
        question_repo: InMemoryScreeningQuestionRepository,
        answer_repo: InMemoryScreeningAnswerRepository,
        vacancy: Vacancy,
        user: User,
        llm: InMemoryLLMClient,
    ) -> None:
        """The LLM response is stored as the answer text for the user."""
        captured: list[str] = []

        def responder(prompt: str) -> str:
            captured.append(prompt)
            return "A thoughtful answer."

        llm._responses = {"*": responder}  # type: ignore[attr-defined]

        question = question_repo.create(
            ScreeningQuestion(vacancy_id=vacancy.id, question_text="Why us?")
        )
        answer = await service.generate_answer(question.id, user.id)

        assert isinstance(answer, ScreeningQuestionAnswer)
        assert answer.answer_text == "A thoughtful answer."
        assert answer.prompt_version == "screening_answer@1.0.0"
        # The prompt that reached the LLM must contain the question text.
        assert captured and "Why us?" in captured[0]
        # And the row was persisted.
        assert answer_repo.get_by_id(answer.id) is answer

    @pytest.mark.asyncio
    async def test_generate_answer_is_idempotent(
        self,
        service: ScreeningService,
        question_repo: InMemoryScreeningQuestionRepository,
        answer_repo: InMemoryScreeningAnswerRepository,
        vacancy: Vacancy,
        user: User,
        llm: InMemoryLLMClient,
    ) -> None:
        """A second call updates the existing row, not a new one."""
        question = question_repo.create(ScreeningQuestion(vacancy_id=vacancy.id, question_text="Q"))

        llm._responses = {"*": "first answer"}  # type: ignore[attr-defined]
        first = await service.generate_answer(question.id, user.id)
        first_id = first.id

        llm._responses = {"*": "second answer"}  # type: ignore[attr-defined]
        second = await service.generate_answer(question.id, user.id)

        assert second.id == first_id
        assert second.answer_text == "second answer"
        assert second.prompt_version == "screening_answer@1.0.0"
        assert second.updated_at is not None

        # And the repo only ever holds one row for this (user, question) pair.
        matches = list(answer_repo.list_by_user_question(user.id, question.id))
        assert len(matches) == 1

    @pytest.mark.asyncio
    async def test_generate_answer_raises_for_missing_question(
        self,
        service: ScreeningService,
        user: User,
    ) -> None:
        """Asking for a non-existent question is a loud failure."""
        with pytest.raises(ScreeningServiceError):
            await service.generate_answer(uuid.uuid4(), user.id)


# ---------------------------------------------------------------------------
# generate_answers_for_vacancy
# ---------------------------------------------------------------------------


class TestGenerateAnswersForVacancy:
    @pytest.mark.asyncio
    async def test_generates_answer_for_every_question(
        self,
        service: ScreeningService,
        question_repo: InMemoryScreeningQuestionRepository,
        vacancy: Vacancy,
        user: User,
    ) -> None:
        """One answer per question, returned in ``question_order`` order."""
        for text in ("a", "b", "c"):
            question_repo.create(
                ScreeningQuestion(vacancy_id=vacancy.id, question_text=text, question_order=0)
            )

        answers = await service.generate_answers_for_vacancy(vacancy.id, user.id)
        assert [a.answer_text for a in answers] == [
            "I am excited to apply because I love building reliable services."
        ] * 3
        assert all(a.user_id == user.id for a in answers)

    @pytest.mark.asyncio
    async def test_returns_empty_list_when_no_questions(
        self,
        service: ScreeningService,
        vacancy: Vacancy,
        user: User,
    ) -> None:
        """No questions means an empty answer list, no LLM calls."""
        assert await service.generate_answers_for_vacancy(vacancy.id, user.id) == []


# ---------------------------------------------------------------------------
# list_user_answers
# ---------------------------------------------------------------------------


class TestListUserAnswers:
    @pytest.mark.asyncio
    async def test_filters_by_user(
        self,
        service: ScreeningService,
        question_repo: InMemoryScreeningQuestionRepository,
        vacancy: Vacancy,
        user: User,
    ) -> None:
        """Other users' answers are never returned."""
        q = question_repo.create(ScreeningQuestion(vacancy_id=vacancy.id, question_text="Q"))
        # Seed a foreign answer directly through the repo.
        other_user_id = uuid.uuid4()
        foreign = ScreeningQuestionAnswer(
            question_id=q.id,
            user_id=other_user_id,
            answer_text="not mine",
            prompt_version="screening_answer@1.0.0",
        )
        from job_apply.features.screening.repository import InMemoryScreeningAnswerRepository

        # Use the same repo the service is wired to so the filter runs.
        assert isinstance(service._answer_repo, InMemoryScreeningAnswerRepository)  # type: ignore[attr-defined]
        service._answer_repo.create(foreign)  # type: ignore[attr-defined]

        # And my own answer via the service so the prompt path is exercised.
        await service.generate_answer(q.id, user.id)

        mine = service.list_user_answers(user.id)
        assert all(a.user_id == user.id for a in mine)
        assert all(a.answer_text != "not mine" for a in mine)

    @pytest.mark.asyncio
    async def test_vacancy_id_filter_narrows_results(
        self,
        service: ScreeningService,
        question_repo: InMemoryScreeningQuestionRepository,
        vacancy_repo: InMemoryVacancyRepository,
        vacancy: Vacancy,
        user: User,
    ) -> None:
        """An optional ``vacancy_id`` narrows the listing to that vacancy."""
        # Build a second vacancy and a question on it.
        v2 = _build_vacancy("Other role")
        vacancy_repo.upsert(v2)
        q1 = question_repo.create(ScreeningQuestion(vacancy_id=vacancy.id, question_text="Q1"))
        q2 = question_repo.create(ScreeningQuestion(vacancy_id=v2.id, question_text="Q2"))
        await service.generate_answer(q1.id, user.id)
        await service.generate_answer(q2.id, user.id)

        only_v1 = service.list_user_answers(user.id, vacancy_id=vacancy.id)
        assert [a.question_id for a in only_v1] == [q1.id]


# Silence unused-import warnings for cross-slice fakes kept available
# for follow-up tests.
_ = SqlVacancyRepository
_ = ResumesRepository
_ = _UserRepoLike
_ = _VacancyRepoLike
_ = _ResumeRepoLike
