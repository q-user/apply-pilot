"""HTTP integration tests for the ``/cover-letter-style`` endpoints.

We use the real FastAPI app wired to a sqlite in-memory engine so the
route handlers, dependency injection, bearer-token authentication, and
validation pipeline are all exercised end-to-end.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import StaticPool, create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from apply_pilot.db import Base, get_db
from apply_pilot.features.cover_letter_style import models  # noqa: F401  -- register table
from apply_pilot.features.cover_letter_style.api import router as cover_letter_style_router
from apply_pilot.features.users import models as _users_models  # noqa: F401
from apply_pilot.features.users.api import router as auth_router


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
def app(engine: Engine) -> Iterator[FastAPI]:
    factory = sessionmaker(bind=engine, class_=Session, autocommit=False, autoflush=False)

    def _override_get_db() -> Iterator[Session]:
        session = factory()
        try:
            yield session
        finally:
            session.close()

    application = FastAPI()
    application.include_router(auth_router)
    application.include_router(cover_letter_style_router)
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
    return _register_and_login(client, "cls-user@example.com", "hunter2!!")


# ---------------------------------------------------------------------------
# GET /cover-letter-style
# ---------------------------------------------------------------------------


def test_get_style_without_token_returns_401(client: TestClient) -> None:
    response = client.get("/cover-letter-style")
    assert response.status_code == 401


def test_get_style_returns_default_when_none_exists(token: str, client: TestClient) -> None:
    """GET must return a default style in the response when none persisted."""
    response = client.get("/cover-letter-style", headers={"Authorization": f"Bearer {token}"})

    assert response.status_code == 200
    body = response.json()
    assert body["tone"] == "professional"
    assert body["length"] == "medium"
    assert body["focus_areas"] == []
    assert body["avoid_phrases"] == []
    assert body["extra_instructions"] is None
    assert body["id"]  # always exposed on the public DTO


def test_get_style_returns_persisted_style(token: str, client: TestClient) -> None:
    """After PUT, GET must return the persisted style."""
    client.put(
        "/cover-letter-style",
        json={
            "tone": "friendly",
            "length": "short",
            "focus_areas": ["teamwork"],
            "avoid_phrases": ["ninja"],
            "extra_instructions": "Be warm.",
        },
        headers={"Authorization": f"Bearer {token}"},
    )

    response = client.get("/cover-letter-style", headers={"Authorization": f"Bearer {token}"})

    assert response.status_code == 200
    body = response.json()
    assert body["tone"] == "friendly"
    assert body["length"] == "short"
    assert body["focus_areas"] == ["teamwork"]
    assert body["avoid_phrases"] == ["ninja"]
    assert body["extra_instructions"] == "Be warm."


# ---------------------------------------------------------------------------
# PUT /cover-letter-style
# ---------------------------------------------------------------------------


def test_put_style_creates_when_none_exists(token: str, client: TestClient) -> None:
    """First PUT must create a style for the caller."""
    response = client.put(
        "/cover-letter-style",
        json={
            "tone": "concise",
            "length": "medium",
            "focus_areas": ["technical_skills", "results"],
        },
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["tone"] == "concise"
    assert body["length"] == "medium"
    assert body["focus_areas"] == ["technical_skills", "results"]
    assert body["id"]


def test_put_style_updates_existing(token: str, client: TestClient) -> None:
    """Subsequent PUTs for the same user must update the same row."""
    first = client.put(
        "/cover-letter-style",
        json={"tone": "friendly", "focus_areas": ["teamwork"]},
        headers={"Authorization": f"Bearer {token}"},
    )
    first_id = first.json()["id"]

    second = client.put(
        "/cover-letter-style",
        json={
            "tone": "formal",
            "length": "long",
            "focus_areas": ["leadership"],
            "avoid_phrases": ["rockstar"],
            "extra_instructions": "Quantify impact.",
        },
        headers={"Authorization": f"Bearer {token}"},
    )

    assert second.status_code == 200
    body = second.json()
    assert body["id"] == first_id
    assert body["tone"] == "formal"
    assert body["length"] == "long"
    assert body["focus_areas"] == ["leadership"]
    assert body["avoid_phrases"] == ["rockstar"]
    assert body["extra_instructions"] == "Quantify impact."


def test_put_style_without_token_returns_401(client: TestClient) -> None:
    response = client.put("/cover-letter-style", json={"tone": "friendly"})
    assert response.status_code == 401


def test_put_style_rejects_invalid_tone(token: str, client: TestClient) -> None:
    """An unknown tone must return 422 from the schema validator."""
    response = client.put(
        "/cover-letter-style",
        json={"tone": "casual-slang"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 422


def test_put_style_rejects_invalid_length(token: str, client: TestClient) -> None:
    """An unknown length must return 422 from the schema validator."""
    response = client.put(
        "/cover-letter-style",
        json={"length": "epic"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 422


def test_put_style_rejects_non_list_focus_areas(token: str, client: TestClient) -> None:
    """focus_areas must be a list of strings."""
    response = client.put(
        "/cover-letter-style",
        json={"focus_areas": "technical_skills"},  # wrong type
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 422


# ---------------------------------------------------------------------------
# DELETE /cover-letter-style
# ---------------------------------------------------------------------------


def test_delete_style_returns_204(token: str, client: TestClient) -> None:
    """Deleting an existing style must return 204 and a subsequent GET returns defaults."""
    client.put(
        "/cover-letter-style",
        json={"tone": "friendly"},
        headers={"Authorization": f"Bearer {token}"},
    )

    response = client.delete("/cover-letter-style", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 204

    # GET must now return defaults again.
    get_resp = client.get("/cover-letter-style", headers={"Authorization": f"Bearer {token}"})
    assert get_resp.status_code == 200
    assert get_resp.json()["tone"] == "professional"


def test_delete_style_is_idempotent(token: str, client: TestClient) -> None:
    """Deleting when no style exists must still return 204 (idempotent)."""
    response = client.delete("/cover-letter-style", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 204


def test_delete_style_without_token_returns_401(client: TestClient) -> None:
    response = client.delete("/cover-letter-style")
    assert response.status_code == 401


# ---------------------------------------------------------------------------
# Cross-user isolation
# ---------------------------------------------------------------------------


def test_styles_are_isolated_per_user(client: TestClient) -> None:
    """Each user must only see their own style."""
    token_a = _register_and_login(client, "user-a@example.com", "hunter2!!")
    token_b = _register_and_login(client, "user-b@example.com", "hunter2!!")

    client.put(
        "/cover-letter-style",
        json={"tone": "friendly"},
        headers={"Authorization": f"Bearer {token_a}"},
    )
    client.put(
        "/cover-letter-style",
        json={"tone": "formal"},
        headers={"Authorization": f"Bearer {token_b}"},
    )

    a = client.get("/cover-letter-style", headers={"Authorization": f"Bearer {token_a}"}).json()
    b = client.get("/cover-letter-style", headers={"Authorization": f"Bearer {token_b}"}).json()

    assert a["tone"] == "friendly"
    assert b["tone"] == "formal"
    assert a["id"] != b["id"]
