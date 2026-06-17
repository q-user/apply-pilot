"""Integration tests for the /auth/* HTTP endpoints.

These tests use the real FastAPI app with a sqlite in-memory engine so
the route handlers, dependency injection, and DB session lifecycle are
exercised end-to-end.
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


def test_register_endpoint_creates_user_and_returns_201(client: TestClient) -> None:
    """POST /auth/register with a fresh email must return 201 and the user body."""
    response = client.post(
        "/auth/register",
        json={"email": "alice@example.com", "password": "hunter2!!"},
    )

    assert response.status_code == 201
    body = response.json()
    assert body["email"] == "alice@example.com"
    assert body["is_active"] is True
    assert body["id"]
    assert "password" not in body
    assert "hashed_password" not in body


def test_register_endpoint_duplicate_email_returns_409(client: TestClient) -> None:
    """A duplicate registration must return HTTP 409 (ConflictError -> 409)."""
    client.post(
        "/auth/register",
        json={"email": "alice@example.com", "password": "hunter2!!"},
    )
    response = client.post(
        "/auth/register",
        json={"email": "alice@example.com", "password": "different-pw"},
    )

    assert response.status_code == 409


def test_register_endpoint_validation_error_returns_422(
    client: TestClient,
) -> None:
    """A malformed payload (missing password) must return HTTP 422."""
    response = client.post("/auth/register", json={"email": "alice@example.com"})

    assert response.status_code == 422


def test_login_endpoint_returns_token_for_correct_password(
    client: TestClient,
) -> None:
    """POST /auth/login with a correct password must return a bearer token."""
    client.post(
        "/auth/register",
        json={"email": "carol@example.com", "password": "hunter2!!"},
    )
    response = client.post(
        "/auth/login",
        json={"email": "carol@example.com", "password": "hunter2!!"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["token_type"] == "bearer"
    assert body["access_token"]


def test_login_endpoint_wrong_password_returns_401(client: TestClient) -> None:
    """POST /auth/login with the wrong password must return HTTP 401."""
    client.post(
        "/auth/register",
        json={"email": "dan@example.com", "password": "hunter2!!"},
    )
    response = client.post(
        "/auth/login",
        json={"email": "dan@example.com", "password": "WRONG-PW"},
    )

    assert response.status_code == 401


def test_login_endpoint_unknown_email_returns_401(client: TestClient) -> None:
    """POST /auth/login with an unknown email must return HTTP 401."""
    response = client.post(
        "/auth/login",
        json={"email": "ghost@example.com", "password": "whatever"},
    )

    assert response.status_code == 401


def test_me_endpoint_returns_user_for_valid_token(client: TestClient) -> None:
    """GET /auth/me with a valid bearer token must return the current user."""
    client.post(
        "/auth/register",
        json={"email": "erin@example.com", "password": "hunter2!!"},
    )
    login = client.post(
        "/auth/login",
        json={"email": "erin@example.com", "password": "hunter2!!"},
    )
    token = login.json()["access_token"]

    response = client.get("/auth/me", headers={"Authorization": f"Bearer {token}"})

    assert response.status_code == 200
    body = response.json()
    assert body["email"] == "erin@example.com"


def test_me_endpoint_without_token_returns_401(client: TestClient) -> None:
    """GET /auth/me without a token must return HTTP 401."""
    response = client.get("/auth/me")

    assert response.status_code == 401


def test_me_endpoint_with_invalid_token_returns_401(client: TestClient) -> None:
    """GET /auth/me with a garbage token must return HTTP 401."""
    response = client.get("/auth/me", headers={"Authorization": "Bearer not-a-real-token"})

    assert response.status_code == 401


def test_logout_endpoint_invalidates_token(client: TestClient) -> None:
    """After POST /auth/logout, the same token must no longer authenticate."""
    client.post(
        "/auth/register",
        json={"email": "frank@example.com", "password": "hunter2!!"},
    )
    login = client.post(
        "/auth/login",
        json={"email": "frank@example.com", "password": "hunter2!!"},
    )
    token = login.json()["access_token"]

    logout = client.post("/auth/logout", headers={"Authorization": f"Bearer {token}"})
    assert logout.status_code == 204

    me_again = client.get("/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert me_again.status_code == 401


def test_logout_endpoint_without_token_returns_401(client: TestClient) -> None:
    """POST /auth/logout without a token must return HTTP 401."""
    response = client.post("/auth/logout")

    assert response.status_code == 401
