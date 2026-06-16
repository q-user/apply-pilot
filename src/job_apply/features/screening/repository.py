"""Persistence gateway for the ``screening`` slice (M3, issue #34).

Two repository surfaces live here, mirroring the convention used by
the ``cover_letter`` and ``cover_letter_style`` slices:

* :class:`ScreeningQuestionRepository` — Protocol defining the
  contract for the questions table.
* :class:`InMemoryScreeningQuestionRepository` — dict-backed fake for
  tests.
* :class:`SqlScreeningQuestionRepository` — production
  implementation backed by a SQLAlchemy ``Session``.

* :class:`ScreeningAnswerRepository` — Protocol for the answers
  table.
* :class:`InMemoryScreeningAnswerRepository` — dict-backed fake.
* :class:`SqlScreeningAnswerRepository` — production implementation.

Idempotency
-----------

The ``ScreeningAnswerRepository.update`` method is the only
mutation path the service uses to rewrite an existing answer. The
service enforces idempotency at a higher level (it looks up the
existing row, then either ``create``s or ``update``s) so the
repository does not need a separate "upsert" primitive. The
``(question_id, user_id)`` unique constraint is the safety net.

``list_by_user`` semantics
--------------------------

The service asks the repository for every answer owned by a user,
optionally filtered by ``vacancy_id``. The SQL implementation joins
through ``screening_questions`` so the optional ``vacancy_id`` filter
is a single query, not an N+1.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from typing import Protocol, runtime_checkable

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from job_apply.features.screening.models import (
    ScreeningQuestion,
    ScreeningQuestionAnswer,
)

# ---------------------------------------------------------------------------
# ScreeningQuestionRepository
# ---------------------------------------------------------------------------


@runtime_checkable
class ScreeningQuestionRepository(Protocol):
    """Minimal interface for the questions table.

    Read methods take vacancy and question ids as plain UUIDs. Write
    methods accept fully-constructed ORM rows; the service is the
    only place that decides which fields to populate.
    """

    def create(self, question: ScreeningQuestion) -> ScreeningQuestion: ...
    def get_by_id(self, question_id: uuid.UUID) -> ScreeningQuestion | None: ...
    def list_by_vacancy(self, vacancy_id: uuid.UUID) -> Sequence[ScreeningQuestion]: ...
    def delete_by_vacancy(self, vacancy_id: uuid.UUID) -> int: ...


class InMemoryScreeningQuestionRepository:
    """Dict-backed repository for tests.

    Stores questions in a single ``_by_id`` dict plus a ``_by_vacancy``
    list so the read methods can answer "all questions for this
    vacancy" without a full scan. The list keeps insertion order;
    the read methods sort by ``question_order`` ascending so the
    behaviour matches the SQL implementation regardless of insertion
    order.
    """

    def __init__(self) -> None:
        self._by_id: dict[uuid.UUID, ScreeningQuestion] = {}
        self._by_vacancy: dict[uuid.UUID, list[uuid.UUID]] = {}

    # -- writers --------------------------------------------------------

    def create(self, question: ScreeningQuestion) -> ScreeningQuestion:
        if question.id is None:
            question.id = uuid.uuid4()
        if question.created_at is None:
            question.created_at = datetime.now(UTC)
        self._by_id[question.id] = question
        self._by_vacancy.setdefault(question.vacancy_id, []).append(question.id)
        return question

    def delete_by_vacancy(self, vacancy_id: uuid.UUID) -> int:
        ids = self._by_vacancy.pop(vacancy_id, [])
        for qid in ids:
            self._by_id.pop(qid, None)
        return len(ids)

    # -- readers --------------------------------------------------------

    def get_by_id(self, question_id: uuid.UUID) -> ScreeningQuestion | None:
        return self._by_id.get(question_id)

    def list_by_vacancy(self, vacancy_id: uuid.UUID) -> Sequence[ScreeningQuestion]:
        ids = self._by_vacancy.get(vacancy_id, [])
        questions = [self._by_id[i] for i in ids if i in self._by_id]
        questions.sort(key=lambda q: q.question_order)
        return questions


class SqlScreeningQuestionRepository:
    """SQLAlchemy-backed repository for :class:`ScreeningQuestion`.

    Construct with either a fixed ``Session`` (caller-managed lifetime)
    or a ``session_factory`` callable (the FastAPI ``get_db`` pattern).
    Each operation opens a short-lived session unless a fixed session
    was supplied.
    """

    def __init__(
        self,
        *,
        session: Session | None = None,
        session_factory: Callable[[], Session] | None = None,
    ) -> None:
        if session is None and session_factory is None:
            raise RuntimeError(
                "SqlScreeningQuestionRepository requires a Session or session_factory"
            )
        self._session = session
        self._session_factory = session_factory

    def _scope(self) -> Session:
        if self._session is not None:
            return self._session
        if self._session_factory is None:
            raise RuntimeError("SqlScreeningQuestionRepository is not bound to a session")
        return self._session_factory()

    # -- writers --------------------------------------------------------

    def create(self, question: ScreeningQuestion) -> ScreeningQuestion:
        session = self._scope()
        try:
            session.add(question)
            session.commit()
            session.refresh(question)
            return question
        except Exception:
            session.rollback()
            raise
        finally:
            if self._session_factory is not None:
                session.close()

    def delete_by_vacancy(self, vacancy_id: uuid.UUID) -> int:
        session = self._scope()
        try:
            statement = delete(ScreeningQuestion).where(ScreeningQuestion.vacancy_id == vacancy_id)
            result = session.execute(statement)
            session.commit()
            # ``rowcount`` is the number of rows matched by the DELETE
            # statement; on sqlite this is the number of rows actually
            # removed.
            return int(result.rowcount or 0)
        except Exception:
            session.rollback()
            raise
        finally:
            if self._session_factory is not None:
                session.close()

    # -- readers --------------------------------------------------------

    def get_by_id(self, question_id: uuid.UUID) -> ScreeningQuestion | None:
        session = self._scope()
        try:
            return session.get(ScreeningQuestion, question_id)
        finally:
            if self._session_factory is not None:
                session.close()

    def list_by_vacancy(self, vacancy_id: uuid.UUID) -> Sequence[ScreeningQuestion]:
        session = self._scope()
        try:
            statement = (
                select(ScreeningQuestion)
                .where(ScreeningQuestion.vacancy_id == vacancy_id)
                .order_by(ScreeningQuestion.question_order.asc(), ScreeningQuestion.id.asc())
            )
            return list(session.execute(statement).scalars().all())
        finally:
            if self._session_factory is not None:
                session.close()


# ---------------------------------------------------------------------------
# ScreeningAnswerRepository
# ---------------------------------------------------------------------------


@runtime_checkable
class ScreeningAnswerRepository(Protocol):
    """Minimal interface for the answers table.

    The optional ``vacancy_id`` argument on :meth:`list_by_user` is the
    only place the service asks the repository to filter across
    tables. The SQL implementation handles the join; the in-memory
    implementation filters the per-user list against a question lookup.
    """

    def create(self, answer: ScreeningQuestionAnswer) -> ScreeningQuestionAnswer: ...
    def get_by_id(self, answer_id: uuid.UUID) -> ScreeningQuestionAnswer | None: ...
    def list_by_user_question(
        self, user_id: uuid.UUID, question_id: uuid.UUID
    ) -> Sequence[ScreeningQuestionAnswer]: ...
    def list_by_user(
        self, user_id: uuid.UUID, *, vacancy_id: uuid.UUID | None = None
    ) -> Sequence[ScreeningQuestionAnswer]: ...
    def update(
        self,
        answer_id: uuid.UUID,
        *,
        answer_text: str,
        prompt_version: str,
        model_used: str | None,
    ) -> ScreeningQuestionAnswer: ...


class InMemoryScreeningAnswerRepository:
    """Dict-backed repository for tests.

    Stores answers in a single ``_by_id`` dict plus per-user and
    per-question indexes so the read methods can answer the various
    filter combinations without a full scan. The per-user list keeps
    insertion order; the per-user-vacancy list is a derived view.
    """

    def __init__(
        self,
        question_lookup: ScreeningQuestionRepository | None = None,
    ) -> None:
        self._by_id: dict[uuid.UUID, ScreeningQuestionAnswer] = {}
        self._by_user: dict[uuid.UUID, list[uuid.UUID]] = {}
        # Optional question lookup so ``list_by_user(vacancy_id=...)``
        # can resolve the vacancy in-memory. When not supplied the
        # ``vacancy_id`` filter is a no-op. Production wiring uses the
        # SQL implementation, which does the join natively.
        self._question_lookup: ScreeningQuestionRepository | None = question_lookup

    def attach_question_lookup(self, question_lookup: ScreeningQuestionRepository) -> None:
        """Attach (or replace) the question lookup after construction.

        The in-memory service uses this so ``list_by_user(vacancy_id=...)``
        can filter without each test having to construct the repo with
        a closure. The SQL implementation does not need this — the
        vacancy filter is pushed down as a JOIN.
        """
        self._question_lookup = question_lookup

    # -- writers --------------------------------------------------------

    def create(self, answer: ScreeningQuestionAnswer) -> ScreeningQuestionAnswer:
        if answer.id is None:
            answer.id = uuid.uuid4()
        if answer.created_at is None:
            answer.created_at = datetime.now(UTC)
        self._by_id[answer.id] = answer
        self._by_user.setdefault(answer.user_id, []).append(answer.id)
        return answer

    def update(
        self,
        answer_id: uuid.UUID,
        *,
        answer_text: str,
        prompt_version: str,
        model_used: str | None,
    ) -> ScreeningQuestionAnswer:
        existing = self._by_id.get(answer_id)
        if existing is None:
            raise KeyError(f"screening question answer {answer_id} not found")
        existing.answer_text = answer_text
        existing.prompt_version = prompt_version
        existing.model_used = model_used
        existing.updated_at = datetime.now(UTC)
        return existing

    # -- readers --------------------------------------------------------

    def get_by_id(self, answer_id: uuid.UUID) -> ScreeningQuestionAnswer | None:
        return self._by_id.get(answer_id)

    def list_by_user_question(
        self, user_id: uuid.UUID, question_id: uuid.UUID
    ) -> Sequence[ScreeningQuestionAnswer]:
        ids = self._by_user.get(user_id, [])
        return [
            self._by_id[i]
            for i in ids
            if i in self._by_id and self._by_id[i].question_id == question_id
        ]

    def list_by_user(
        self, user_id: uuid.UUID, *, vacancy_id: uuid.UUID | None = None
    ) -> Sequence[ScreeningQuestionAnswer]:
        ids = self._by_user.get(user_id, [])
        answers = [self._by_id[i] for i in ids if i in self._by_id]
        if vacancy_id is None or self._question_lookup is None:
            return answers
        filtered: list[ScreeningQuestionAnswer] = []
        for answer in answers:
            question = self._question_lookup.get_by_id(answer.question_id)
            if question is not None and question.vacancy_id == vacancy_id:
                filtered.append(answer)
        return filtered


class SqlScreeningAnswerRepository:
    """SQLAlchemy-backed repository for :class:`ScreeningQuestionAnswer`.

    The optional ``vacancy_id`` filter on :meth:`list_by_user` joins
    through :class:`ScreeningQuestion` so the query stays single-shot.
    """

    def __init__(
        self,
        *,
        session: Session | None = None,
        session_factory: Callable[[], Session] | None = None,
    ) -> None:
        if session is None and session_factory is None:
            raise RuntimeError("SqlScreeningAnswerRepository requires a Session or session_factory")
        self._session = session
        self._session_factory = session_factory

    def _scope(self) -> Session:
        if self._session is not None:
            return self._session
        if self._session_factory is None:
            raise RuntimeError("SqlScreeningAnswerRepository is not bound to a session")
        return self._session_factory()

    # -- writers --------------------------------------------------------

    def create(self, answer: ScreeningQuestionAnswer) -> ScreeningQuestionAnswer:
        session = self._scope()
        try:
            session.add(answer)
            session.commit()
            session.refresh(answer)
            return answer
        except Exception:
            session.rollback()
            raise
        finally:
            if self._session_factory is not None:
                session.close()

    def update(
        self,
        answer_id: uuid.UUID,
        *,
        answer_text: str,
        prompt_version: str,
        model_used: str | None,
    ) -> ScreeningQuestionAnswer:
        session = self._scope()
        try:
            existing = session.get(ScreeningQuestionAnswer, answer_id)
            if existing is None:
                raise KeyError(f"screening question answer {answer_id} not found")
            existing.answer_text = answer_text
            existing.prompt_version = prompt_version
            existing.model_used = model_used
            existing.updated_at = datetime.now(UTC)
            session.commit()
            session.refresh(existing)
            return existing
        except Exception:
            session.rollback()
            raise
        finally:
            if self._session_factory is not None:
                session.close()

    # -- readers --------------------------------------------------------

    def get_by_id(self, answer_id: uuid.UUID) -> ScreeningQuestionAnswer | None:
        session = self._scope()
        try:
            return session.get(ScreeningQuestionAnswer, answer_id)
        finally:
            if self._session_factory is not None:
                session.close()

    def list_by_user_question(
        self, user_id: uuid.UUID, question_id: uuid.UUID
    ) -> Sequence[ScreeningQuestionAnswer]:
        session = self._scope()
        try:
            statement = select(ScreeningQuestionAnswer).where(
                ScreeningQuestionAnswer.user_id == user_id,
                ScreeningQuestionAnswer.question_id == question_id,
            )
            return list(session.execute(statement).scalars().all())
        finally:
            if self._session_factory is not None:
                session.close()

    def list_by_user(
        self, user_id: uuid.UUID, *, vacancy_id: uuid.UUID | None = None
    ) -> Sequence[ScreeningQuestionAnswer]:
        session = self._scope()
        try:
            statement = select(ScreeningQuestionAnswer).where(
                ScreeningQuestionAnswer.user_id == user_id
            )
            if vacancy_id is not None:
                statement = statement.join(
                    ScreeningQuestion,
                    ScreeningQuestion.id == ScreeningQuestionAnswer.question_id,
                ).where(ScreeningQuestion.vacancy_id == vacancy_id)
            statement = statement.order_by(
                ScreeningQuestionAnswer.created_at.asc(),
                ScreeningQuestionAnswer.id.asc(),
            )
            return list(session.execute(statement).scalars().all())
        finally:
            if self._session_factory is not None:
                session.close()


__all__ = [
    "InMemoryScreeningAnswerRepository",
    "InMemoryScreeningQuestionRepository",
    "ScreeningAnswerRepository",
    "ScreeningQuestionRepository",
    "SqlScreeningAnswerRepository",
    "SqlScreeningQuestionRepository",
]
