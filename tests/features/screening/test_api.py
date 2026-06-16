"""Integration tests for the /screening/* HTTP endpoints (M3, issue #34).

These tests stand up a FastAPI app with the screening router mounted,
wire a sqlite in-memory database, and exercise the full request /
response cycle through :class:`fastapi.testclient.TestClient`.
Authentication uses the existing ``/auth/register`` + ``/auth/login``
flow so the bearer-token plumbing is the real one from the users
slice.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import StaticPool, create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from job_apply.db import Base, get_db
from job_apply.features.scoring.llm import InMemoryLLMClient
from job_apply.features.screening import models as _screening_models  # noqa: F401
from job_apply.features.screening.api import router as screening_router
from job_apply.features.screening.repository import (
    InMemoryScreeningAnswerRepository,
    InMemoryScreeningQuestionRepository,
)
from job_apply.features.screening.service import ScreeningService
from job_apply.features.sources.models import Vacancy
from job_apply.features.users import models as _users_models  # noqa: F401
from job_apply.features.users.api import router as auth_router
from job_apply.features.users.repository import InMemoryUsersRepository


def _register_and_login(client: TestClient, email: str, password: str) -> str:
    resp = client.post("/auth/register", json={"email": email, "password": password})
    assert resp.status_code == 201, resp.json()
    resp = client.post("/auth/login", json={"email": email, "password": password})
    assert resp.status_code == 200, resp.json()
    return resp.json()["access_token"]


@pytest.fixture
def engine() -> Iterator[Engine]:
    eng = create_engine(
        "sqlite:///:memory:",
        future=True,
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=eng)
    try:
        yield eng
    finally:
        eng.dispose()


@pytest.fixture
def session_factory(engine: Engine):
    return sessionmaker(bind=engine, class_=Session, autocommit=False, autoflush=False)


@pytest.fixture
def screening_state() -> dict:
    """Bag of state shared between the screening router and the tests.

    The screening router in this slice is wired with in-memory
    repositories (a separate in-memory ``Question`` / ``Answer`` store)
    so the tests do not need a second SQL session per request. The
    vacancies themselves still live in the SQL engine because the
    endpoint takes ``vacancy_id`` and the service looks them up.
    """
    return {
        "llm": InMemoryLLMClient(responses={"*": "Auto-generated answer."}),
        "question_repo": InMemoryScreeningQuestionRepository(),
        "answer_repo": InMemoryScreeningAnswerRepository(),
        "user_repo": InMemoryUsersRepository(),
    }


@pytest.fixture
def app(session_factory, screening_state) -> Iterator[FastAPI]:
    def _override_get_db() -> Iterator[Session]:
        session = session_factory()
        try:
            yield session
        finally:
            session.close()

    application = FastAPI()
    application.include_router(auth_router)
    application.include_router(screening_router)
    application.dependency_overrides[get_db] = _override_get_db

    # Wire the screening service with the in-memory repos shared with
    # the test through the ``screening_state`` fixture.
    from job_apply.features.screening.api import get_screening_service

    # The in-memory answer repo needs a question lookup to filter
    # ``list_by_user(vacancy_id=...)``. The SQL implementation does the
    # join natively, so this wiring is a test-only concern.
    screening_state["answer_repo"].attach_question_lookup(screening_state["question_repo"])

    service = ScreeningService(
        llm=screening_state["llm"],
        question_repo=screening_state["question_repo"],
        answer_repo=screening_state["answer_repo"],
        user_repo=screening_state["user_repo"],
        resume_repo=_NoResumeRepository(),
        vacancy_repo=_SqlVacancyRepositoryShim(session_factory),
    )
    application.dependency_overrides[get_screening_service] = lambda: service

    try:
        yield application
    finally:
        application.dependency_overrides.clear()


@pytest.fixture
def client(app: FastAPI) -> Iterator[TestClient]:
    with TestClient(app) as c:
        yield c


@pytest.fixture
def token(client: TestClient) -> str:
    return _register_and_login(client, "screening-user@example.com", "hunter2!!")


@pytest.fixture
def vacancy_id(session_factory) -> uuid.UUID:
    """Insert a vacancy directly and return its id."""
    session = session_factory()
    try:
        vacancy = Vacancy(
            id=uuid.uuid4(),
            source="hh",
            source_id="hh-screening-api-1",
            title="Senior Python Dev",
            raw_data={"id": "hh-screening-api-1", "name": "Senior Python Dev"},
        )
        session.add(vacancy)
        session.commit()
        return vacancy.id
    finally:
        session.close()


# ---------------------------------------------------------------------------
# Local fakes
# ---------------------------------------------------------------------------


class _NoResumeRepository:
    """Always returns no resumes — answer generation will fall back gracefully."""

    def list_for_user(self, user_id: uuid.UUID) -> list:
        return []


class _SqlVacancyRepositoryShim:
    """Thin shim that exposes ``get_by_id`` for the screening service."""

    def __init__(self, session_factory) -> None:
        self._session_factory = session_factory

    def get_by_id(self, vacancy_id: uuid.UUID) -> Vacancy | None:
        session = self._session_factory()
        try:
            return session.get(Vacancy, vacancy_id)
        finally:
            session.close()


# ---------------------------------------------------------------------------
# Auth gate
# ---------------------------------------------------------------------------


def test_endpoints_require_token(client: TestClient, vacancy_id: uuid.UUID) -> None:
    """Every screening endpoint must reject requests without a bearer token."""
    assert client.post(f"/screening/questions/{vacancy_id}").status_code == 401
    assert client.get("/screening/answers").status_code == 401
    assert client.post(f"/screening/questions/{uuid.uuid4()}/answer").status_code == 401


# ---------------------------------------------------------------------------
# POST /screening/questions/{vacancy_id}
# ---------------------------------------------------------------------------


def test_add_questions_persists_and_returns_them(
    client: TestClient, token: str, vacancy_id: uuid.UUID
) -> None:
    """Adding questions returns the persisted rows in insertion order."""
    response = client.post(
        f"/screening/questions/{vacancy_id}",
        json={"questions": ["Why us?", "Years of experience?"]},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 201, response.json()
    body = response.json()
    assert [q["question_text"] for q in body] == ["Why us?", "Years of experience?"]
    assert [q["question_order"] for q in body] == [0, 1]
    assert all(q["vacancy_id"] == str(vacancy_id) for q in body)


def test_add_questions_rejects_empty_body(
    client: TestClient, token: str, vacancy_id: uuid.UUID
) -> None:
    """A body without a ``questions`` list is a 422 from Pydantic."""
    response = client.post(
        f"/screening/questions/{vacancy_id}",
        json={},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 422


# ---------------------------------------------------------------------------
# POST /screening/questions/{question_id}/answer
# ---------------------------------------------------------------------------


def test_generate_answer_creates_and_returns_row(
    client: TestClient,
    token: str,
    vacancy_id: uuid.UUID,
    screening_state,
) -> None:
    """Generating an answer persists a row keyed by (user, question)."""
    add_resp = client.post(
        f"/screening/questions/{vacancy_id}",
        json={"questions": ["Why us?"]},
        headers={"Authorization": f"Bearer {token}"},
    )
    question_id = add_resp.json()[0]["id"]

    gen_resp = client.post(
        f"/screening/questions/{question_id}/answer",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert gen_resp.status_code == 201, gen_resp.json()
    body = gen_resp.json()
    assert body["answer_text"] == "Auto-generated answer."
    assert body["prompt_version"] == "screening_answer@1.0.0"

    # A second call updates the same row, not a new one.
    gen_resp_2 = client.post(
        f"/screening/questions/{question_id}/answer",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert gen_resp_2.status_code == 201
    assert gen_resp_2.json()["id"] == body["id"]


def test_generate_answer_404_for_missing_question(client: TestClient, token: str) -> None:
    """Asking for a non-existent question id returns 404."""
    response = client.post(
        f"/screening/questions/{uuid.uuid4()}/answer",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 404


# ---------------------------------------------------------------------------
# GET /screening/answers
# ---------------------------------------------------------------------------


def test_list_answers_returns_only_callers(
    client: TestClient,
    token: str,
    other_token_factory,
    vacancy_id: uuid.UUID,
    screening_state,
) -> None:
    """Each user only sees their own answers."""
    other_token = other_token_factory("screening-other@example.com")

    # Add one question, answer it as both users.
    add_resp = client.post(
        f"/screening/questions/{vacancy_id}",
        json={"questions": ["Q"]},
        headers={"Authorization": f"Bearer {token}"},
    )
    question_id = add_resp.json()[0]["id"]
    client.post(
        f"/screening/questions/{question_id}/answer",
        headers={"Authorization": f"Bearer {token}"},
    )
    client.post(
        f"/screening/questions/{question_id}/answer",
        headers={"Authorization": f"Bearer {other_token}"},
    )

    my_resp = client.get("/screening/answers", headers={"Authorization": f"Bearer {token}"})
    assert my_resp.status_code == 200
    body = my_resp.json()
    assert len(body) == 1
    # Decoding the bearer token gives the user id used to filter.
    from job_apply.features.users.security import default_token_store

    user_id_str = default_token_store().resolve(token)
    assert body[0]["user_id"] == user_id_str


def test_list_answers_filters_by_vacancy_id(
    client: TestClient,
    token: str,
    vacancy_id: uuid.UUID,
    screening_state,
    session_factory,
) -> None:
    """The optional ``vacancy_id`` query param narrows the listing."""
    # Seed a second vacancy directly so the filter has another row to exclude.
    other_vacancy_id = uuid.uuid4()
    session = session_factory()
    try:
        other_vacancy = Vacancy(
            id=other_vacancy_id,
            source="hh",
            source_id="hh-screening-api-2",
            title="Other role",
            raw_data={"id": "hh-screening-api-2", "name": "Other role"},
        )
        session.add(other_vacancy)
        session.commit()
    finally:
        session.close()

    # Add one question on each vacancy and answer both.
    first = _add_one_question(client, token, vacancy_id, "Q1")
    second = _add_one_question(client, token, other_vacancy_id, "Q2")
    client.post(
        f"/screening/questions/{first}/answer", headers={"Authorization": f"Bearer {token}"}
    )
    client.post(
        f"/screening/questions/{second}/answer", headers={"Authorization": f"Bearer {token}"}
    )

    only_v1 = client.get(
        f"/screening/answers?vacancy_id={vacancy_id}",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert only_v1.status_code == 200
    body = only_v1.json()
    assert len(body) == 1
    # The returned answer must belong to the requested vacancy.
    question = screening_state["question_repo"].get_by_id(uuid.UUID(body[0]["question_id"]))
    assert question is not None
    assert question.vacancy_id == vacancy_id


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def other_token_factory(client: TestClient):
    def _make(email: str) -> str:
        return _register_and_login(client, email, "hunter2!!")

    return _make


def _add_one_question(client: TestClient, token: str, vacancy_id: uuid.UUID, text: str) -> str:
    response = client.post(
        f"/screening/questions/{vacancy_id}",
        json={"questions": [text]},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 201, response.json()
    return response.json()[0]["id"]


# Silence unused-import warnings for the screening_router fixture
# parameter — the router is mounted inside the FastAPI app.
_ = screening_router  # re-exported via fixtures
