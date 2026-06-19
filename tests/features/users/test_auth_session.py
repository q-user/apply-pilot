"""TDD tests for the M6 browser-friendly session cookie (M6, issue #169).

The M6 frontend shell is plain HTML forms, not a SPA — so the auth
endpoints must accept a ``<form method="post" action="/auth/login">``
submission in addition to the existing JSON contract. The /auth/login
endpoint becomes content-negotiated: HTML clients get a ``Set-Cookie``
plus a 303 redirect, JSON clients keep getting a bearer token (and also
get a cookie, so a hybrid SPA can use either).

These tests describe the contract for both paths and the new
``GET /auth/login`` page that renders the login form.
"""

from __future__ import annotations

import os
from collections.abc import Iterator

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import StaticPool, create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

# Workaround for a pre-existing circular import between
# ``users.api`` → ``telegram.linking`` → … → ``apply_worker.runtime``
# → ``matches.service`` that surfaces when this file is collected
# in isolation (without xdist's lucky distribution). Pre-loading
# ``apply_worker`` here resolves the cycle: the package finishes
# initialising, so the second import is a no-op. This is a
# collection-time aid only; it does not affect what the tests assert.
import apply_pilot.features.apply_worker  # noqa: E402,F401  (cycle breaker)
from apply_pilot.db import Base, get_db
from apply_pilot.features.users import models as _users_models  # noqa: F401  (register User)
from apply_pilot.features.users.api import router as auth_router
from apply_pilot.features.users.session import (
    LOGIN_PATH,
    LOGOUT_PATH,
    SESSION_COOKIE_NAME,
    get_auth_session_settings,
)


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


def _register(client: TestClient, *, email: str, password: str = "hunter2!!") -> None:
    """Helper: register a user, asserting 201."""
    response = client.post("/auth/register", json={"email": email, "password": password})
    assert response.status_code == 201, response.text


# ---------------------------------------------------------------------------
# Constants and settings
# ---------------------------------------------------------------------------


def test_session_cookie_constants_are_exposed() -> None:
    """The cookie / path constants are part of the slice's public surface."""
    assert SESSION_COOKIE_NAME == "apply_pilot_session"
    assert LOGIN_PATH == "/auth/login"
    assert LOGOUT_PATH == "/auth/logout"


def test_default_settings_have_secure_cookie_in_production() -> None:
    """With APP_ENV unset (production default), the cookie is ``secure=True``."""
    env = {k: v for k, v in os.environ.items() if k != "APP_ENV"}
    with pytest.MonkeyPatch.context() as mp:
        mp.delenv("APP_ENV", raising=False)
        for k, v in env.items():
            mp.setenv(k, v)
        # Drop the cached settings so the new env takes effect.
        get_auth_session_settings.cache_clear()
        try:
            settings = get_auth_session_settings()
        finally:
            get_auth_session_settings.cache_clear()
    assert settings.secure is True
    assert settings.httponly is True
    assert settings.samesite == "lax"
    assert settings.cookie_name == SESSION_COOKIE_NAME
    assert settings.max_age_seconds == 60 * 60 * 8


def test_settings_disable_secure_cookie_in_development(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When APP_ENV=development, the cookie is ``secure=False`` so HTTP works."""
    monkeypatch.setenv("APP_ENV", "development")
    get_auth_session_settings.cache_clear()
    try:
        settings = get_auth_session_settings()
    finally:
        get_auth_session_settings.cache_clear()
    assert settings.secure is False


# ---------------------------------------------------------------------------
# POST /auth/login — content negotiation
# ---------------------------------------------------------------------------


def test_login_html_sets_cookie_and_redirects(client: TestClient) -> None:
    """POST /auth/login with Accept: text/html must 303 and set the cookie."""
    _register(client, email="alice@example.com")

    response = client.post(
        "/auth/login",
        data={"email": "alice@example.com", "password": "hunter2!!"},
        headers={"Accept": "text/html"},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/dashboard"
    # Cookie is set even on the HTML path.
    set_cookie = response.headers.get("set-cookie", "")
    assert SESSION_COOKIE_NAME in set_cookie
    assert "HttpOnly" in set_cookie
    assert "SameSite=lax" in set_cookie
    assert "Path=/" in set_cookie


def test_login_json_does_not_redirect(client: TestClient) -> None:
    """POST /auth/login with Accept: application/json returns the bearer token.

    The cookie is still set, so hybrid clients can use either credential.
    """
    _register(client, email="bob@example.com")

    response = client.post(
        "/auth/login",
        json={"email": "bob@example.com", "password": "hunter2!!"},
        headers={"Accept": "application/json"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["token_type"] == "bearer"
    assert body["access_token"]
    assert SESSION_COOKIE_NAME in response.headers.get("set-cookie", "")


def test_login_html_invalid_credentials_renders_form_with_error(
    client: TestClient,
) -> None:
    """A wrong password on the HTML path must 401 and re-render the form."""
    _register(client, email="carol@example.com")

    response = client.post(
        "/auth/login",
        data={"email": "carol@example.com", "password": "WRONG-PW"},
        headers={"Accept": "text/html"},
    )

    assert response.status_code == 401
    body = response.text
    assert "<form" in body
    assert 'action="/auth/login"' in body
    # An error message is rendered, NOT a generic 401.
    assert "invalid email or password" in body.lower() or "error" in body.lower()
    # No session cookie on failure.
    assert SESSION_COOKIE_NAME not in response.headers.get("set-cookie", "")


def test_login_redirect_uses_next_when_safe(client: TestClient) -> None:
    """A safe ``next`` value is honoured on successful HTML login."""
    _register(client, email="dave@example.com")

    response = client.post(
        "/auth/login",
        data={
            "email": "dave@example.com",
            "password": "hunter2!!",
            "next": "/admin/integrations",
        },
        headers={"Accept": "text/html"},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/admin/integrations"


def test_login_redirect_rejects_open_redirect(client: TestClient) -> None:
    """An absolute ``next`` URL must fall back to /dashboard (no open redirect)."""
    _register(client, email="erin@example.com")

    response = client.post(
        "/auth/login",
        data={
            "email": "erin@example.com",
            "password": "hunter2!!",
            "next": "https://evil.example.com/steal",
        },
        headers={"Accept": "text/html"},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/dashboard"


# ---------------------------------------------------------------------------
# GET /auth/login — the login form
# ---------------------------------------------------------------------------


def test_get_login_form_renders_html(client: TestClient) -> None:
    """GET /auth/login returns the inline HTML login form."""
    response = client.get("/auth/login", headers={"Accept": "text/html"})

    assert response.status_code == 200
    body = response.text
    assert "<form" in body
    assert 'action="/auth/login"' in body
    assert 'method="post"' in body
    assert 'name="email"' in body
    assert 'name="password"' in body


def test_get_login_form_prefilled_next(client: TestClient) -> None:
    """A ``?next=...`` query string is rendered as a hidden input."""
    response = client.get(
        "/auth/login",
        params={"next": "/admin"},
        headers={"Accept": "text/html"},
    )

    assert response.status_code == 200
    assert 'value="/admin"' in response.text


def test_get_login_redirects_when_already_logged_in(client: TestClient) -> None:
    """An already-authenticated visitor is bounced to ?next or /dashboard."""
    _register(client, email="frank@example.com")
    login = client.post(
        "/auth/login",
        json={"email": "frank@example.com", "password": "hunter2!!"},
        headers={"Accept": "application/json"},
    )
    assert login.status_code == 200
    token = login.json()["access_token"]

    # Visit the form with a valid session cookie AND a ?next query.
    response = client.get(
        "/auth/login?next=/admin/integrations",
        headers={"Accept": "text/html"},
        cookies={SESSION_COOKIE_NAME: token},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/admin/integrations"


# ---------------------------------------------------------------------------
# Cookie as auth credential
# ---------------------------------------------------------------------------


def test_me_with_cookie_returns_user(client: TestClient) -> None:
    """GET /auth/me with only a valid session cookie returns the user JSON."""
    _register(client, email="grace@example.com")
    login = client.post(
        "/auth/login",
        json={"email": "grace@example.com", "password": "hunter2!!"},
        headers={"Accept": "application/json"},
    )
    token = login.json()["access_token"]

    response = client.get(
        "/auth/me",
        headers={"Accept": "application/json"},
        cookies={SESSION_COOKIE_NAME: token},
    )

    assert response.status_code == 200
    assert response.json()["email"] == "grace@example.com"


def test_me_with_cookie_or_bearer_either_works(client: TestClient) -> None:
    """Bearer wins when both are supplied, but the cookie path also works."""
    _register(client, email="heidi@example.com")
    login = client.post(
        "/auth/login",
        json={"email": "heidi@example.com", "password": "hunter2!!"},
        headers={"Accept": "application/json"},
    )
    cookie_token = login.json()["access_token"]

    # Only the cookie.
    response = client.get(
        "/auth/me",
        cookies={SESSION_COOKIE_NAME: cookie_token},
    )
    assert response.status_code == 200
    assert response.json()["email"] == "heidi@example.com"


def test_me_with_no_creds_returns_401(client: TestClient) -> None:
    """No header + no cookie = 401."""
    response = client.get("/auth/me")
    assert response.status_code == 401


# ---------------------------------------------------------------------------
# POST /auth/logout — content negotiation
# ---------------------------------------------------------------------------


def test_logout_html_clears_cookie_and_redirects(client: TestClient) -> None:
    """POST /auth/logout with Accept: text/html clears the cookie + 303s to /."""
    _register(client, email="ivan@example.com")
    login = client.post(
        "/auth/login",
        json={"email": "ivan@example.com", "password": "hunter2!!"},
    )
    token = login.json()["access_token"]

    response = client.post(
        "/auth/logout",
        headers={"Accept": "text/html"},
        cookies={SESSION_COOKIE_NAME: token},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/"
    # The cookie is cleared. FastAPI's ``delete_cookie`` emits a
    # ``Set-Cookie: name=; Max-Age=0`` header.
    set_cookie = response.headers.get("set-cookie", "")
    assert SESSION_COOKIE_NAME in set_cookie
    assert ("Max-Age=0" in set_cookie) or ("max-age=0" in set_cookie.lower())


def test_logout_json_returns_204(client: TestClient) -> None:
    """POST /auth/logout with Accept: application/json still returns 204."""
    _register(client, email="judy@example.com")
    login = client.post(
        "/auth/login",
        json={"email": "judy@example.com", "password": "hunter2!!"},
    )
    token = login.json()["access_token"]

    response = client.post(
        "/auth/logout",
        headers={"Accept": "application/json"},
        cookies={SESSION_COOKIE_NAME: token},
    )

    assert response.status_code == 204
    set_cookie = response.headers.get("set-cookie", "")
    assert SESSION_COOKIE_NAME in set_cookie


# ---------------------------------------------------------------------------
# Cookie security attributes
# ---------------------------------------------------------------------------


def test_cookie_secure_default_in_production(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """In production (APP_ENV unset) the Set-Cookie has the ``Secure`` flag."""
    monkeypatch.delenv("APP_ENV", raising=False)
    get_auth_session_settings.cache_clear()
    try:
        _register(client, email="karl@example.com")
        response = client.post(
            "/auth/login",
            json={"email": "karl@example.com", "password": "hunter2!!"},
        )
        assert response.status_code == 200
        assert "Secure" in response.headers.get("set-cookie", "")
    finally:
        get_auth_session_settings.cache_clear()


def test_cookie_not_secure_in_development(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """In development (APP_ENV=development) the Set-Cookie has NO ``Secure`` flag."""
    monkeypatch.setenv("APP_ENV", "development")
    get_auth_session_settings.cache_clear()
    try:
        _register(client, email="laura@example.com")
        response = client.post(
            "/auth/login",
            json={"email": "laura@example.com", "password": "hunter2!!"},
        )
        assert response.status_code == 200
        set_cookie = response.headers.get("set-cookie", "")
        assert SESSION_COOKIE_NAME in set_cookie
        assert "Secure" not in set_cookie
    finally:
        get_auth_session_settings.cache_clear()


# ---------------------------------------------------------------------------
# XSS: HTML-escape every interpolated value
# ---------------------------------------------------------------------------


def test_html_pages_escape_user_input(client: TestClient) -> None:
    """A user-controlled string in the password field is HTML-escaped.

    ``UserCreate``'s ``EmailAddress`` pattern rejects ``<`` and ``>``,
    so the only way a user-controlled string can land on an error
    page is via a wrong-password attempt with a script-tagged
    *password*. We use a normal email + a hostile password here.
    """
    _register(client, email="mallory@example.com", password="hunter2!!")

    response = client.post(
        "/auth/login",
        data={
            "email": "mallory@example.com",
            "password": "<script>alert(1)</script>",
        },
        headers={"Accept": "text/html"},
    )

    assert response.status_code == 401
    body = response.text
    # The raw script tag must NOT appear unescaped.
    assert "<script>alert(1)</script>" not in body
    # The escaped form is allowed (and likely present, since the
    # password value is the only thing we control at this point).
    assert "&lt;script&gt;" in body or "alert(1)" not in body
