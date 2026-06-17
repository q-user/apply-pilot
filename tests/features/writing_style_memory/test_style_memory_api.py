"""HTTP integration tests for the ``/writing-style-memory/me`` endpoint.

We mount the new router on a real FastAPI app wired to a sqlite in-memory
engine so the route handler, bearer-token authentication, and
dependency-injected service are all exercised end-to-end.
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

from apply_pilot.db import Base, get_db
from apply_pilot.features.cover_letter import models as _cover_letter_models  # noqa: F401
from apply_pilot.features.users import models as _users_models  # noqa: F401
from apply_pilot.features.users.api import router as auth_router
from apply_pilot.features.users.security import default_token_store
from apply_pilot.features.writing_style_memory import models  # noqa: F401  -- register table
from apply_pilot.features.writing_style_memory.api import router as writing_style_memory_router
from apply_pilot.features.writing_style_memory.repository import SqlStyleMemoryRepository
from apply_pilot.features.writing_style_memory.service import StyleMemoryService


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
def session_factory(engine: Engine) -> Iterator[sessionmaker[Session]]:
    yield sessionmaker(bind=engine, class_=Session, autocommit=False, autoflush=False)


@pytest.fixture
def app(session_factory: sessionmaker[Session]) -> Iterator[FastAPI]:
    def _override_get_db() -> Iterator[Session]:
        session = session_factory()
        try:
            yield session
        finally:
            session.close()

    application = FastAPI()
    application.include_router(auth_router)
    application.include_router(writing_style_memory_router)
    application.dependency_overrides[get_db] = _override_get_db

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
    return _register_and_login(client, "style-memory-user@example.com", "hunter2!!")


# ---------------------------------------------------------------------------
# GET /writing-style-memory/me
# ---------------------------------------------------------------------------


def test_get_summary_without_token_returns_401(client: TestClient) -> None:
    response = client.get("/writing-style-memory/me")
    assert response.status_code == 401


def test_get_summary_returns_null_when_no_entries(token: str, client: TestClient) -> None:
    """An empty style memory must surface as ``null`` to the API caller."""
    response = client.get("/writing-style-memory/me", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 200
    body = response.json()
    assert body["aggregated_summary"] is None
    assert body["user_id"]  # always present, even on empty


def test_get_summary_returns_aggregated_text(
    token: str, client: TestClient, session_factory: sessionmaker[Session]
) -> None:
    """An existing style memory must return the aggregated summary string."""
    user_id = uuid.UUID(default_token_store().resolve(token))
    with session_factory() as session:
        repo = SqlStyleMemoryRepository(session=session)
        service = StyleMemoryService(repository=repo)
        service.record_accepted_letter(
            user_id=user_id,
            cover_letter_id=uuid.uuid4(),
            letter_text="Hello there! I bring ten years of Python experience.",
        )

    response = client.get("/writing-style-memory/me", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 200
    body = response.json()
    assert body["user_id"] == str(user_id)
    assert body["aggregated_summary"] is not None
    assert "first-sentence:" in body["aggregated_summary"]


def test_get_summary_isolates_users(
    client: TestClient, session_factory: sessionmaker[Session]
) -> None:
    """Two users' style memories must not leak into each other."""
    token_a = _register_and_login(client, "style-a@example.com", "hunter2!!")
    token_b = _register_and_login(client, "style-b@example.com", "hunter2!!")

    user_a_id = uuid.UUID(default_token_store().resolve(token_a))

    with session_factory() as session:
        service = StyleMemoryService(repository=SqlStyleMemoryRepository(session=session))
        service.record_accepted_letter(
            user_id=user_a_id,
            cover_letter_id=uuid.uuid4(),
            letter_text="A private summary.",
        )

    # User B has no entries.
    response_b = client.get(
        "/writing-style-memory/me", headers={"Authorization": f"Bearer {token_b}"}
    )
    assert response_b.status_code == 200
    assert response_b.json()["aggregated_summary"] is None

    # User A sees their entry.
    response_a = client.get(
        "/writing-style-memory/me", headers={"Authorization": f"Bearer {token_a}"}
    )
    assert response_a.status_code == 200
    assert response_a.json()["aggregated_summary"] is not None
    assert "A private summary" in response_a.json()["aggregated_summary"]
