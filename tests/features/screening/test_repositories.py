"""TDD tests for the screening-question repository (M3, issue #34).

Two repository surfaces are tested:

* :class:`ScreeningQuestionRepository` — stores the questions attached
  to a vacancy. Read paths answer ``list_by_vacancy``; writes answer
  ``create`` and bulk-delete via ``delete_by_vacancy``.
* :class:`ScreeningAnswerRepository` — stores the per-user answers.
  Read paths answer ``list_by_user_question`` and
  ``list_by_user``; writes answer ``create`` and ``update``.

Both :class:`InMemory*` and ``Sql*`` implementations are exercised.
The SQL tests run against an in-memory sqlite engine (no real DB)
and use the production model + repository to keep the contract
honest.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import StaticPool, create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from job_apply.db import Base
from job_apply.features.screening import models as _screening_models  # noqa: F401
from job_apply.features.screening.models import (
    ScreeningQuestion,
    ScreeningQuestionAnswer,
)
from job_apply.features.screening.repository import (
    InMemoryScreeningAnswerRepository,
    InMemoryScreeningQuestionRepository,
    SqlScreeningAnswerRepository,
    SqlScreeningQuestionRepository,
)
from job_apply.features.sources.models import Vacancy
from job_apply.features.users.models import User
from job_apply.features.users.security import hash_password

# ---------------------------------------------------------------------------
# In-memory: ScreeningQuestionRepository
# ---------------------------------------------------------------------------


class TestInMemoryScreeningQuestionRepository:
    def test_create_and_get_by_id(self) -> None:
        """A freshly-created question must be retrievable by id."""
        repo = InMemoryScreeningQuestionRepository()
        vacancy_id = uuid.uuid4()
        q = ScreeningQuestion(vacancy_id=vacancy_id, question_text="Why us?")
        created = repo.create(q)
        assert created.id is not None
        assert repo.get_by_id(created.id) is created

    def test_list_by_vacancy_returns_only_matching(self) -> None:
        """Questions from another vacancy must not leak into the listing."""
        repo = InMemoryScreeningQuestionRepository()
        v1, v2 = uuid.uuid4(), uuid.uuid4()
        repo.create(ScreeningQuestion(vacancy_id=v1, question_text="a", question_order=1))
        repo.create(ScreeningQuestion(vacancy_id=v1, question_text="b", question_order=2))
        repo.create(ScreeningQuestion(vacancy_id=v2, question_text="c", question_order=1))

        v1_questions = list(repo.list_by_vacancy(v1))
        assert {q.question_text for q in v1_questions} == {"a", "b"}

    def test_list_by_vacancy_preserves_question_order(self) -> None:
        """Questions must come back sorted by ``question_order`` ascending."""
        repo = InMemoryScreeningQuestionRepository()
        v = uuid.uuid4()
        repo.create(ScreeningQuestion(vacancy_id=v, question_text="second", question_order=2))
        repo.create(ScreeningQuestion(vacancy_id=v, question_text="first", question_order=1))
        repo.create(ScreeningQuestion(vacancy_id=v, question_text="third", question_order=3))

        ordered = [q.question_text for q in repo.list_by_vacancy(v)]
        assert ordered == ["first", "second", "third"]

    def test_delete_by_vacancy_removes_all_matching(self) -> None:
        """``delete_by_vacancy`` wipes every question for the given vacancy."""
        repo = InMemoryScreeningQuestionRepository()
        v1, v2 = uuid.uuid4(), uuid.uuid4()
        repo.create(ScreeningQuestion(vacancy_id=v1, question_text="a"))
        repo.create(ScreeningQuestion(vacancy_id=v1, question_text="b"))
        repo.create(ScreeningQuestion(vacancy_id=v2, question_text="keep"))

        deleted = repo.delete_by_vacancy(v1)
        assert deleted == 2
        assert list(repo.list_by_vacancy(v1)) == []
        # The other vacancy's questions are untouched.
        assert {q.question_text for q in repo.list_by_vacancy(v2)} == {"keep"}


# ---------------------------------------------------------------------------
# In-memory: ScreeningAnswerRepository
# ---------------------------------------------------------------------------


class TestInMemoryScreeningAnswerRepository:
    def _user(self) -> uuid.UUID:
        return uuid.uuid4()

    def _question(self) -> uuid.UUID:
        return uuid.uuid4()

    def test_create_and_get_by_id(self) -> None:
        """A freshly-created answer must be retrievable by id."""
        repo = InMemoryScreeningAnswerRepository()
        user_id = self._user()
        question_id = self._question()
        answer = ScreeningQuestionAnswer(
            question_id=question_id,
            user_id=user_id,
            answer_text="Because I love the team.",
            prompt_version="screening_answer@1.0.0",
        )
        created = repo.create(answer)
        assert created.id is not None
        assert repo.get_by_id(created.id) is created

    def test_list_by_user_question_filters_both_keys(self) -> None:
        """Answers for the same question by other users must be filtered out."""
        repo = InMemoryScreeningAnswerRepository()
        user_a, user_b = self._user(), self._user()
        question_id = self._question()

        repo.create(
            ScreeningQuestionAnswer(
                question_id=question_id,
                user_id=user_a,
                answer_text="A",
                prompt_version="v",
            )
        )
        repo.create(
            ScreeningQuestionAnswer(
                question_id=question_id,
                user_id=user_b,
                answer_text="B",
                prompt_version="v",
            )
        )

        a_answers = list(repo.list_by_user_question(user_a, question_id))
        assert [a.answer_text for a in a_answers] == ["A"]

    def test_list_by_user_returns_only_users_answers(self) -> None:
        """Listing by user must not surface another user's answers."""
        repo = InMemoryScreeningAnswerRepository()
        user_a, user_b = self._user(), self._user()
        repo.create(
            ScreeningQuestionAnswer(
                question_id=self._question(),
                user_id=user_a,
                answer_text="mine",
                prompt_version="v",
            )
        )
        repo.create(
            ScreeningQuestionAnswer(
                question_id=self._question(),
                user_id=user_b,
                answer_text="theirs",
                prompt_version="v",
            )
        )

        mine = list(repo.list_by_user(user_a))
        assert [a.answer_text for a in mine] == ["mine"]

    def test_update_modifies_text_and_prompt_version(self) -> None:
        """``update`` rewrites the answer text, prompt version, and model."""
        repo = InMemoryScreeningAnswerRepository()
        user_id = self._user()
        question_id = self._question()
        created = repo.create(
            ScreeningQuestionAnswer(
                question_id=question_id,
                user_id=user_id,
                answer_text="first",
                prompt_version="screening_answer@1.0.0",
            )
        )

        updated = repo.update(
            created.id,
            answer_text="second",
            prompt_version="screening_answer@1.1.0",
            model_used="gpt-4o",
        )
        assert updated.answer_text == "second"
        assert updated.prompt_version == "screening_answer@1.1.0"
        assert updated.model_used == "gpt-4o"
        assert updated.updated_at is not None


# ---------------------------------------------------------------------------
# SQL: shared fixture
# ---------------------------------------------------------------------------


@pytest.fixture
def engine() -> Engine:
    """Yield a fresh in-memory sqlite engine with the screening tables created."""
    eng = create_engine(
        "sqlite:///:memory:",
        future=True,
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=eng)
    yield eng
    eng.dispose()


@pytest.fixture
def session_factory(engine: Engine):
    return sessionmaker(bind=engine, class_=Session, autocommit=False, autoflush=False)


_user_counter = 0
_vacancy_counter = 0


def _seed_user_and_vacancy(session_factory) -> tuple[uuid.UUID, uuid.UUID]:
    """Persist a user + vacancy and return their ids (screening FKs need both).

    Each call uses a unique ``email`` / ``source_id`` so the unique
    constraints in :class:`User` and :class:`Vacancy` do not fire when
    the test seeds more than one row.
    """
    global _user_counter, _vacancy_counter
    _user_counter += 1
    _vacancy_counter += 1
    session = session_factory()
    try:
        user = User(
            id=uuid.uuid4(),
            email=f"screening{_user_counter}@example.com",
            hashed_password=hash_password("hunter2!!"),
        )
        vacancy = Vacancy(
            id=uuid.uuid4(),
            source="hh",
            source_id=f"hh-screening-{_vacancy_counter}",
            title="Senior Python Dev",
            raw_data={"id": f"hh-screening-{_vacancy_counter}"},
        )
        session.add(user)
        session.add(vacancy)
        session.commit()
        return user.id, vacancy.id
    finally:
        session.close()


# ---------------------------------------------------------------------------
# SQL: ScreeningQuestionRepository
# ---------------------------------------------------------------------------


class TestSqlScreeningQuestionRepository:
    def test_create_and_list_round_trip(self, session_factory) -> None:
        """A question created via SQL must come back via the list path."""
        _, vacancy_id = _seed_user_and_vacancy(session_factory)
        repo = SqlScreeningQuestionRepository(session_factory=session_factory)

        repo.create(ScreeningQuestion(vacancy_id=vacancy_id, question_text="Q1", question_order=1))
        repo.create(ScreeningQuestion(vacancy_id=vacancy_id, question_text="Q2", question_order=2))

        questions = list(repo.list_by_vacancy(vacancy_id))
        assert [q.question_text for q in questions] == ["Q1", "Q2"]

    def test_delete_by_vacancy_wipes_only_matching(self, session_factory) -> None:
        """``delete_by_vacancy`` is scoped to the given vacancy id."""
        _, v1 = _seed_user_and_vacancy(session_factory)
        # Need a second vacancy; insert one more row directly. The
        # counter guarantees a unique ``source_id`` so the unique
        # constraint on ``(source, source_id)`` does not fire.
        session = session_factory()
        try:
            global _vacancy_counter
            _vacancy_counter += 1
            second_id = f"hh-screening-extra-{_vacancy_counter}"
            v2 = Vacancy(
                id=uuid.uuid4(),
                source="hh",
                source_id=second_id,
                title="Other",
                raw_data={"id": second_id},
            )
            session.add(v2)
            session.commit()
            v2_id = v2.id
        finally:
            session.close()

        repo = SqlScreeningQuestionRepository(session_factory=session_factory)
        repo.create(ScreeningQuestion(vacancy_id=v1, question_text="a"))
        repo.create(ScreeningQuestion(vacancy_id=v1, question_text="b"))
        repo.create(ScreeningQuestion(vacancy_id=v2_id, question_text="keep"))

        deleted = repo.delete_by_vacancy(v1)
        assert deleted == 2
        assert list(repo.list_by_vacancy(v1)) == []
        assert len(list(repo.list_by_vacancy(v2_id))) == 1


# ---------------------------------------------------------------------------
# SQL: ScreeningAnswerRepository
# ---------------------------------------------------------------------------


class TestSqlScreeningAnswerRepository:
    def test_create_and_update_round_trip(self, session_factory) -> None:
        """An answer can be created and then updated via the repo."""
        user_id, vacancy_id = _seed_user_and_vacancy(session_factory)
        question_repo = SqlScreeningQuestionRepository(session_factory=session_factory)
        answer_repo = SqlScreeningAnswerRepository(session_factory=session_factory)

        q = question_repo.create(ScreeningQuestion(vacancy_id=vacancy_id, question_text="Q"))
        created = answer_repo.create(
            ScreeningQuestionAnswer(
                question_id=q.id,
                user_id=user_id,
                answer_text="first",
                prompt_version="screening_answer@1.0.0",
            )
        )

        updated = answer_repo.update(
            created.id,
            answer_text="second",
            prompt_version="screening_answer@1.0.0",
            model_used="gpt-4o",
        )
        assert updated.answer_text == "second"
        assert updated.model_used == "gpt-4o"

    def test_list_by_user_question_filters(self, session_factory) -> None:
        """``list_by_user_question`` returns only the matching pair."""
        user_a_id, vacancy_id = _seed_user_and_vacancy(session_factory)
        user_b_id = _seed_user_and_vacancy(session_factory)[0]
        question_repo = SqlScreeningQuestionRepository(session_factory=session_factory)
        answer_repo = SqlScreeningAnswerRepository(session_factory=session_factory)

        q = question_repo.create(ScreeningQuestion(vacancy_id=vacancy_id, question_text="Q"))
        answer_repo.create(
            ScreeningQuestionAnswer(
                question_id=q.id,
                user_id=user_a_id,
                answer_text="A",
                prompt_version="v",
            )
        )
        answer_repo.create(
            ScreeningQuestionAnswer(
                question_id=q.id,
                user_id=user_b_id,
                answer_text="B",
                prompt_version="v",
            )
        )

        a_answers = list(answer_repo.list_by_user_question(user_a_id, q.id))
        assert [a.answer_text for a in a_answers] == ["A"]

    def test_unique_constraint_on_question_user_pair(self, session_factory) -> None:
        """The (question_id, user_id) pair must be unique at the DB level."""
        user_id, vacancy_id = _seed_user_and_vacancy(session_factory)
        question_repo = SqlScreeningQuestionRepository(session_factory=session_factory)
        answer_repo = SqlScreeningAnswerRepository(session_factory=session_factory)
        q = question_repo.create(ScreeningQuestion(vacancy_id=vacancy_id, question_text="Q"))
        answer_repo.create(
            ScreeningQuestionAnswer(
                question_id=q.id,
                user_id=user_id,
                answer_text="first",
                prompt_version="v",
            )
        )
        with pytest.raises(IntegrityError):  # unique constraint on sqlite
            answer_repo.create(
                ScreeningQuestionAnswer(
                    question_id=q.id,
                    user_id=user_id,
                    answer_text="second",
                    prompt_version="v",
                )
            )

    def test_list_by_user_filters_other_users(self, session_factory) -> None:
        """``list_by_user`` must not return another user's answers."""
        user_a_id, vacancy_id = _seed_user_and_vacancy(session_factory)
        user_b_id = _seed_user_and_vacancy(session_factory)[0]
        question_repo = SqlScreeningQuestionRepository(session_factory=session_factory)
        answer_repo = SqlScreeningAnswerRepository(session_factory=session_factory)

        q = question_repo.create(ScreeningQuestion(vacancy_id=vacancy_id, question_text="Q"))
        answer_repo.create(
            ScreeningQuestionAnswer(
                question_id=q.id,
                user_id=user_a_id,
                answer_text="A",
                prompt_version="v",
            )
        )
        answer_repo.create(
            ScreeningQuestionAnswer(
                question_id=q.id,
                user_id=user_b_id,
                answer_text="B",
                prompt_version="v",
            )
        )

        a_answers = list(answer_repo.list_by_user(user_a_id))
        assert [a.answer_text for a in a_answers] == ["A"]
