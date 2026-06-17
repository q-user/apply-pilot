"""Integration tests for GET /auth/telegram-link endpoint.

Follows the same pattern as ``test_auth_api.py``: sqlite in-memory engine,
a minimal FastAPI app with the auth router, and TestClient.
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
from apply_pilot.features.users import models as _users_models  # noqa: F401  (register User)
from apply_pilot.features.users.api import router as auth_router


@pytest.fixture
def engine() -> Iterator[Engine]:
    """Build a fresh in-memory sqlite engine per test, with all tables created."""
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
    """Build a FastAPI app wired to the in-memory engine."""
    factory = sessionmaker(bind=engine, class_=Session, autocommit=False, autoflush=False)

    def _override_get_db() -> Iterator[Session]:
        session = factory()
        try:
            yield session
        finally:
            session.close()

    application = FastAPI()
    application.include_router(auth_router)
    application.dependency_overrides[get_db] = _override_get_db

    try:
        yield application
    finally:
        application.dependency_overrides.clear()


@pytest.fixture
def client(app: FastAPI) -> Iterator[TestClient]:
    """TestClient bound to the per-test FastAPI app."""
    with TestClient(app) as c:
        yield c


def _register_and_login(client: TestClient, email: str, password: str) -> str:
    """Create a user and return a valid bearer token."""
    client.post("/auth/register", json={"email": email, "password": password})
    login_resp = client.post("/auth/login", json={"email": email, "password": password})
    assert login_resp.status_code == 200
    return login_resp.json()["access_token"]


def test_telegram_link_endpoint_returns_linking_code(client: TestClient) -> None:
    """GET /auth/telegram-link must return a linking code for an authenticated user."""
    token = _register_and_login(client, "linker@example.com", "hunter2!!")

    response = client.get(
        "/auth/telegram-link",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 200
    body = response.json()
    assert "linking_code" in body
    assert len(body["linking_code"]) > 0


def test_telegram_link_endpoint_without_token_returns_401(client: TestClient) -> None:
    """GET /auth/telegram-link without a token must return 401."""
    response = client.get("/auth/telegram-link")
    assert response.status_code == 401


def test_telegram_link_endpoint_with_invalid_token_returns_401(
    client: TestClient,
) -> None:
    """GET /auth/telegram-link with a garbage token must return 401."""
    response = client.get(
        "/auth/telegram-link",
        headers={"Authorization": "Bearer not-a-real-token"},
    )
    assert response.status_code == 401


def test_telegram_link_endpoint_returns_different_codes(
    client: TestClient,
) -> None:
    """Multiple calls from the same user should return different codes."""
    token = _register_and_login(client, "unique-linker@example.com", "hunter2!!")

    resp1 = client.get(
        "/auth/telegram-link",
        headers={"Authorization": f"Bearer {token}"},
    )
    resp2 = client.get(
        "/auth/telegram-link",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert resp1.status_code == 200
    assert resp2.status_code == 200
    # Each call generates a new code; the old one should be replaced
    code1 = resp1.json()["linking_code"]
    code2 = resp2.json()["linking_code"]
    assert code1 != code2
