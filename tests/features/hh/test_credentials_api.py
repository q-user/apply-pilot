"""Integration tests for the /hh/credentials HTTP endpoints.

These tests use a FastAPI TestClient with a sqlite in-memory engine,
exercising the full dependency chain: router → service → repository → encryptor.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import StaticPool, create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from job_apply.db import Base, get_db
from job_apply.features.hh.api import router as hh_router
from job_apply.features.users import models as _users_models  # noqa: F401
from job_apply.features.users.api import router as auth_router


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
    application.include_router(hh_router)
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


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _register_and_login(client: TestClient) -> str:
    """Register a user and return a bearer token."""
    client.post(
        "/auth/register",
        json={"email": "hh-test@example.com", "password": "hunter2!!"},
    )
    resp = client.post(
        "/auth/login",
        json={"email": "hh-test@example.com", "password": "hunter2!!"},
    )
    assert resp.status_code == 200
    return resp.json()["access_token"]


# ---------------------------------------------------------------------------
# POST /hh/credentials
# ---------------------------------------------------------------------------


def test_post_credentials_stores_and_returns_201(client: TestClient) -> None:
    """POST /hh/credentials with valid payload must store credentials and return 201
    with redacted metadata."""
    token = _register_and_login(client)
    expires = (datetime.now(UTC) + timedelta(hours=1)).isoformat()

    resp = client.post(
        "/hh/credentials",
        json={
            "access_token": "hh-access-token-value",
            "refresh_token": "hh-refresh-token-value",
            "expires_at": expires,
        },
        headers={"Authorization": f"Bearer {token}"},
    )

    assert resp.status_code == 201
    body = resp.json()
    assert body["token_type"] == "bearer"
    assert body["expires_at"] is not None
    assert body["access_token"] == "REDACTED"
    assert body["refresh_token"] == "REDACTED"


def test_post_credentials_requires_auth(client: TestClient) -> None:
    """POST /hh/credentials without a bearer token must return 401."""
    resp = client.post(
        "/hh/credentials",
        json={"access_token": "tok", "refresh_token": None, "expires_at": None},
    )
    assert resp.status_code == 401


def test_post_credentials_without_refresh_token(client: TestClient) -> None:
    """POST /hh/credentials with refresh_token=null is valid."""
    token = _register_and_login(client)

    resp = client.post(
        "/hh/credentials",
        json={"access_token": "just-access", "refresh_token": None, "expires_at": None},
        headers={"Authorization": f"Bearer {token}"},
    )

    assert resp.status_code == 201
    body = resp.json()
    assert body["access_token"] == "REDACTED"
    assert body["refresh_token"] == "REDACTED"


# ---------------------------------------------------------------------------
# GET /hh/credentials
# ---------------------------------------------------------------------------


def test_get_credentials_returns_metadata_only(client: TestClient) -> None:
    """GET /hh/credentials must return metadata (token_type, expires_at) without raw tokens."""
    token = _register_and_login(client)
    expires = (datetime.now(UTC) + timedelta(hours=1)).isoformat()

    client.post(
        "/hh/credentials",
        json={
            "access_token": "top-secret-access",
            "refresh_token": "top-secret-refresh",
            "expires_at": expires,
        },
        headers={"Authorization": f"Bearer {token}"},
    )

    resp = client.get(
        "/hh/credentials",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["has_credentials"] is True
    assert body["token_type"] == "bearer"
    assert body["expires_at"] is not None
    # No raw tokens exposed
    assert "access_token" not in body
    assert "refresh_token" not in body
    assert "top-secret" not in str(body)


def test_get_credentials_not_found(client: TestClient) -> None:
    """GET /hh/credentials for a user with no stored credentials returns has_credentials=False."""
    token = _register_and_login(client)

    resp = client.get(
        "/hh/credentials",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["has_credentials"] is False
    assert body["token_type"] is None
    assert body["expires_at"] is None


def test_get_credentials_requires_auth(client: TestClient) -> None:
    """GET /hh/credentials without a bearer token must return 401."""
    resp = client.get("/hh/credentials")
    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# DELETE /hh/credentials
# ---------------------------------------------------------------------------


def test_delete_credentials_removes_and_returns_204(client: TestClient) -> None:
    """DELETE /hh/credentials must remove stored credentials and return 204."""
    token = _register_and_login(client)

    client.post(
        "/hh/credentials",
        json={"access_token": "tok", "refresh_token": None, "expires_at": None},
        headers={"Authorization": f"Bearer {token}"},
    )

    resp = client.delete(
        "/hh/credentials",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 204

    # Verify they are gone
    get_resp = client.get(
        "/hh/credentials",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert get_resp.json()["has_credentials"] is False


def test_delete_credentials_not_found_is_noop(client: TestClient) -> None:
    """DELETE /hh/credentials when none exist must still return 204."""
    token = _register_and_login(client)

    resp = client.delete(
        "/hh/credentials",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 204


def test_delete_credentials_requires_auth(client: TestClient) -> None:
    """DELETE /hh/credentials without a bearer token must return 401."""
    resp = client.delete("/hh/credentials")
    assert resp.status_code == 401
