"""Integration tests for the /hh/oauth HTTP endpoints (issue #19).

These tests wire the OAuth slice into a FastAPI app with an in-memory
SQLite engine and an in-memory OAuth client. The OAuth client is
exchanged with a fake before the request lands so the slice never tries
to call the real hh.ru endpoints.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import StaticPool, create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from job_apply.db import Base, get_db
from job_apply.features.hh.api import (
    get_hh_oauth_client,
    get_hh_oauth_state_store,
)
from job_apply.features.hh.api import (
    router as hh_router,
)
from job_apply.features.hh.oauth import (
    HhOAuthStateStore,
    HhTokenResponse,
    InMemoryHhOAuthClient,
)
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

    # Override the OAuth dependencies so we never talk to a real network.
    state_store = HhOAuthStateStore()
    oauth_client = InMemoryHhOAuthClient(client_id="CID")

    def _override_state_store() -> HhOAuthStateStore:
        return state_store

    def _override_oauth_client() -> InMemoryHhOAuthClient:
        return oauth_client

    application.dependency_overrides[get_hh_oauth_state_store] = _override_state_store
    application.dependency_overrides[get_hh_oauth_client] = _override_oauth_client

    # Stash the fakes on the app so tests can introspect / pre-load them.
    application.state.oauth_state_store = state_store
    application.state.oauth_client = oauth_client

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
        json={"email": "oauth-test@example.com", "password": "hunter2!!"},
    )
    resp = client.post(
        "/auth/login",
        json={"email": "oauth-test@example.com", "password": "hunter2!!"},
    )
    assert resp.status_code == 200
    return resp.json()["access_token"]


def _oauth_fakes(app: FastAPI) -> tuple[HhOAuthStateStore, InMemoryHhOAuthClient]:
    return app.state.oauth_state_store, app.state.oauth_client


# ---------------------------------------------------------------------------
# GET /hh/oauth/authorize
# ---------------------------------------------------------------------------


def test_authorize_returns_url_and_state(client: TestClient, app: FastAPI) -> None:
    """GET /hh/oauth/authorize must return a JSON body with
    authorization_url and state. The state must be registered against
    the authenticated user so the callback can resolve it back."""
    token = _register_and_login(client)
    state_store, _ = _oauth_fakes(app)

    resp = client.get(
        "/hh/oauth/authorize",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert resp.status_code == 200
    body = resp.json()
    assert "authorization_url" in body
    assert "state" in body
    assert "hh.ru/oauth/authorize" in body["authorization_url"]
    assert body["state"] in state_store._states  # noqa: SLF001 - introspection for tests


def test_authorize_requires_auth(client: TestClient) -> None:
    """GET /hh/oauth/authorize without a bearer token must return 401."""
    resp = client.get("/hh/oauth/authorize")
    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# GET /hh/oauth/callback
# ---------------------------------------------------------------------------


def test_callback_stores_credentials_and_returns_metadata(client: TestClient, app: FastAPI) -> None:
    """GET /hh/oauth/callback with a valid state and pre-armed code must
    exchange the code, store the tokens, and return redacted metadata."""
    token = _register_and_login(client)
    state_store, oauth_client = _oauth_fakes(app)

    # First, ask the authorize endpoint for a state.
    auth_resp = client.get(
        "/hh/oauth/authorize",
        headers={"Authorization": f"Bearer {token}"},
    )
    state = auth_resp.json()["state"]

    # Arm the in-memory OAuth client with the response for our code.
    oauth_client._exchange["AUTH-CODE"] = HhTokenResponse(  # noqa: SLF001
        access_token="FRESH-ACCESS",
        refresh_token="FRESH-REFRESH",
        token_type="bearer",
        expires_in=3600,
        scope=None,
    )

    # hh.ru will redirect the user back to our callback URL.
    resp = client.get(
        "/hh/oauth/callback",
        params={"code": "AUTH-CODE", "state": state},
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["token_type"] == "bearer"
    assert body["expires_at"] is not None
    assert body["access_token"] == "REDACTED"
    assert body["refresh_token"] == "REDACTED"
    # user_id appears for client convenience.
    assert "user_id" in body

    # The state was consumed.
    assert state not in state_store._states  # noqa: SLF001


def test_callback_rejects_unknown_state(client: TestClient) -> None:
    """A callback with a state that was never issued must return 400
    and store nothing."""
    _register_and_login(client)

    resp = client.get(
        "/hh/oauth/callback",
        params={"code": "ANY", "state": "never-issued"},
    )

    assert resp.status_code == 400
    body = resp.json()
    assert body["detail"]["code"] == "invalid_oauth_state"


def test_callback_missing_code_returns_422(client: TestClient) -> None:
    """A callback with no ``code`` query param must fail validation
    (Pydantic/Query validation -> 422)."""
    resp = client.get(
        "/hh/oauth/callback",
        params={"state": "any"},
    )
    # FastAPI returns 422 when a required query param is missing.
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# POST /hh/oauth/refresh
# ---------------------------------------------------------------------------


def test_refresh_endpoint_refreshes_credentials(client: TestClient, app: FastAPI) -> None:
    """POST /hh/oauth/refresh must read the stored refresh token, call
    the OAuth client, and update the stored credentials."""
    token = _register_and_login(client)
    _, oauth_client = _oauth_fakes(app)

    # Pre-arm: seed credentials via the existing /hh/credentials
    # endpoint so we have a refresh token to use.
    store_resp = client.post(
        "/hh/credentials",
        json={
            "access_token": "OLD-ACCESS",
            "refresh_token": "OLD-REFRESH",
            "expires_at": None,
        },
        headers={"Authorization": f"Bearer {token}"},
    )
    assert store_resp.status_code == 201

    # Arm the in-memory OAuth client to return new tokens.
    oauth_client._refresh["OLD-REFRESH"] = HhTokenResponse(  # noqa: SLF001
        access_token="NEW-ACCESS",
        refresh_token="NEW-REFRESH",
        token_type="bearer",
        expires_in=7200,
        scope=None,
    )

    resp = client.post(
        "/hh/oauth/refresh",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["token_type"] == "bearer"
    assert body["access_token"] == "REDACTED"
    assert body["refresh_token"] == "REDACTED"


def test_refresh_endpoint_requires_auth(client: TestClient) -> None:
    """POST /hh/oauth/refresh without a bearer token must return 401."""
    resp = client.post("/hh/oauth/refresh")
    assert resp.status_code == 401


def test_refresh_endpoint_404_when_no_credentials(client: TestClient) -> None:
    """A refresh with no stored credentials must return 404 (no row
    to update)."""
    token = _register_and_login(client)

    resp = client.post(
        "/hh/oauth/refresh",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert resp.status_code == 404


def test_refresh_endpoint_400_when_no_refresh_token(client: TestClient, app: FastAPI) -> None:
    """Stored credentials without a refresh token must yield a 400
    (re-authorize required)."""
    token = _register_and_login(client)

    # Seed credentials with no refresh_token.
    client.post(
        "/hh/credentials",
        json={
            "access_token": "JUST-ACCESS",
            "refresh_token": None,
            "expires_at": None,
        },
        headers={"Authorization": f"Bearer {token}"},
    )

    resp = client.post(
        "/hh/oauth/refresh",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert resp.status_code == 400
    body = resp.json()
    assert body["detail"]["code"] == "missing_refresh_token"
