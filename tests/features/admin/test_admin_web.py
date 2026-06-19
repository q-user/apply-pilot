"""TDD tests for the admin HTML surface (M6, issue #171).

The admin slice grows three HTML routes:

* ``GET /admin/``              — landing page with nav + sign-out form.
* ``GET /admin/integrations``  — read-only HTML view of integration
  health snapshots.
* ``GET /admin/users``         — paginated HTML view of every user.

The auth model is tightened: every admin route accepts the session
cookie introduced in PR #170 as a fallback to ``Authorization: Bearer``,
and the :func:`require_admin_user` dependency now refuses non-admin
users with ``403``. The legacy ``/admin/health`` JSON behaviour is
preserved (issue #145) — the new gate is additive.

Tests in this module drive the slice end-to-end through a
:class:`fastapi.testclient.TestClient` wired to the real
:func:`apply_pilot.app.create_app` factory, with the SQLAlchemy session
overridden to an in-memory sqlite engine and the integration store /
worker replaced with in-memory fakes.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import suppress
from datetime import UTC, datetime

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import StaticPool, create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from apply_pilot.app import create_app
from apply_pilot.config import get_admin_auth_required
from apply_pilot.db import Base, get_db
from apply_pilot.features.admin.api import get_integration_status_store
from apply_pilot.features.admin.integrations import (
    InMemoryIntegrationStatusStore,
    IntegrationStatus,
)
from apply_pilot.features.users import models as _users_models  # noqa: F401  (register User)
from apply_pilot.features.users.session import SESSION_COOKIE_NAME

# ---------------------------------------------------------------------------
# Engine / session helpers
# ---------------------------------------------------------------------------


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
def app(
    engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[FastAPI]:
    """Build the full FastAPI app with an in-memory sqlite engine.

    The :func:`get_admin_auth_required` dependency is flipped to
    ``True`` so the production gate (token + ``is_admin``) is
    exercised. The integration store is replaced with a fresh
    in-memory store so ``/admin/integrations`` has deterministic
    contents. ``APP_ENV`` is set to ``development`` so the session
    cookie's ``secure`` flag is ``False``; otherwise the
    :class:`fastapi.testclient.TestClient` (plain HTTP) refuses to
    carry the cookie on subsequent requests, and every admin route
    would 303 back to ``/auth/login``.
    """
    monkeypatch.setenv("APP_ENV", "development")
    # The settings module caches :func:`get_auth_session_settings` with
    # ``lru_cache``; clear the cache so the env change takes effect.
    from apply_pilot.features.users.session import get_auth_session_settings

    get_auth_session_settings.cache_clear()

    factory = sessionmaker(bind=engine, class_=Session, autocommit=False, autoflush=False)

    def _override_get_db() -> Iterator[Session]:
        session = factory()
        try:
            yield session
        finally:
            session.close()

    application = create_app()
    application.dependency_overrides[get_db] = _override_get_db
    application.dependency_overrides[get_admin_auth_required] = lambda: True

    # Replace the default in-process integration store with a fresh
    # one. We do NOT configure a worker — the page just reads what
    # the store currently holds.
    fresh_store = InMemoryIntegrationStatusStore()
    application.dependency_overrides[get_integration_status_store] = lambda: fresh_store

    try:
        yield application
    finally:
        application.dependency_overrides.clear()


@pytest.fixture
def client(app: FastAPI) -> Iterator[TestClient]:
    with TestClient(app) as c:
        yield c


# ---------------------------------------------------------------------------
# Auth helpers
# ---------------------------------------------------------------------------


def _register(client: TestClient, *, email: str, password: str = "hunter2!!") -> None:
    """Helper: register a user, asserting 201."""
    response = client.post("/auth/register", json={"email": email, "password": password})
    assert response.status_code == 201, response.text


def _login_html(client: TestClient, *, email: str, password: str = "hunter2!!") -> TestClient:
    """Log a user in via the HTML path and return the same client (cookie set).

    Returns the same :class:`TestClient` so subsequent calls carry the
    session cookie. The :class:`TestClient` keeps cookies across
    requests, so callers can simply chain off the returned value.
    """
    response = client.post(
        "/auth/login",
        data={"email": email, "password": password},
        headers={"Accept": "text/html"},
        follow_redirects=False,
    )
    assert response.status_code == 303, response.text
    assert SESSION_COOKIE_NAME in response.headers.get("set-cookie", "")
    return client


def _promote_admin(*, email: str, session: Session) -> None:
    """Flip ``is_admin=True`` on the user with *email* directly in the DB.

    The CLI bootstrap path is exercised in
    :mod:`tests.features.users.test_admin_promotion`. These HTML
    tests just need an admin user; writing through the session is the
    fastest way to get there without coupling this file to the CLI.
    """
    from apply_pilot.features.users.models import User

    user = session.query(User).filter_by(email=email).one()
    user.is_admin = True
    session.commit()


def _promote_admin_via_app(app: FastAPI, *, email: str) -> None:
    """Open a session through the app's overridden ``get_db`` and promote.

    Used by tests that need an admin user but want to avoid the
    ``with client.app.dependency_overrides[get_db]() as ...`` context
    manager dance (FastAPI's ``get_db`` is a generator, not a
    context manager).
    """
    from apply_pilot.features.users.models import User

    get_db_override = app.dependency_overrides[get_db]
    gen = get_db_override()
    session = next(iter(gen))
    try:
        user = session.query(User).filter_by(email=email).one()
        user.is_admin = True
        session.commit()
    finally:
        with suppress(StopIteration):
            next(gen)


# ---------------------------------------------------------------------------
# GET /admin/  — landing page
# ---------------------------------------------------------------------------


def test_admin_landing_redirects_when_unauthenticated(client: TestClient) -> None:
    """A visitor with no session is bounced to the login page."""
    response = client.get("/admin/", headers={"Accept": "text/html"}, follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == "/auth/login?next=/admin/"


def test_admin_landing_renders_when_admin(client: TestClient, app: FastAPI) -> None:
    """A logged-in admin sees the landing page with nav and identity."""
    _register(client, email="ops@example.com")
    # Promote the user — same call the new ``promote`` CLI makes.
    _promote_admin_via_app(app, email="ops@example.com")

    client.cookies.clear()
    _login_html(client, email="ops@example.com")

    response = client.get("/admin/", headers={"Accept": "text/html"}, follow_redirects=False)

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    body = response.text
    assert "<h1>Admin</h1>" in body or ">Admin<" in body
    assert "/admin/health" in body
    assert "/admin/integrations" in body
    assert "/admin/users" in body
    assert "ops@example.com" in body
    # The sign-out form posts to /auth/logout.
    assert 'action="/auth/logout"' in body


def test_admin_landing_forbidden_when_authenticated_but_not_admin(
    client: TestClient,
) -> None:
    """A logged-in but non-admin user gets a 403 page, not the dashboard."""
    _register(client, email="regular@example.com")
    _login_html(client, email="regular@example.com")

    response = client.get("/admin/", headers={"Accept": "text/html"}, follow_redirects=False)

    assert response.status_code == 403
    assert response.headers["content-type"].startswith("text/html")
    body = response.text
    # The forbidden page tells the visitor they need admin access.
    assert "admin" in body.lower()


# ---------------------------------------------------------------------------
# GET /admin/integrations  — HTML table
# ---------------------------------------------------------------------------


def test_admin_integrations_renders_table(client: TestClient, app: FastAPI) -> None:
    """The integrations page renders an HTML table row per snapshot."""
    # Pre-seed the store with three snapshots so the page has rows.
    store = app.dependency_overrides[get_integration_status_store]()
    now = datetime.now(UTC)
    for name, status_value in (("hh", "healthy"), ("llm", "degraded"), ("db", "unhealthy")):
        store.update(
            name,
            IntegrationStatus(
                name=name,
                status=status_value,
                last_checked_at=now,
                error=None if status_value == "healthy" else f"{name} down",
                metadata=None,
            ),
        )

    _register(client, email="ops@example.com")
    _promote_admin_via_app(app, email="ops@example.com")

    client.cookies.clear()
    _login_html(client, email="ops@example.com")

    response = client.get(
        "/admin/integrations", headers={"Accept": "text/html"}, follow_redirects=False
    )

    assert response.status_code == 200
    body = response.text
    assert "<table" in body
    assert "<tbody" in body
    # One row per status snapshot.
    assert body.count("<tr") == len(store.get_all()) + 1  # +1 for the header row
    for snapshot in store.get_all():
        assert snapshot.name in body


# ---------------------------------------------------------------------------
# GET /admin/users  — paginated HTML table
# ---------------------------------------------------------------------------


def _register_n_users(client: TestClient, *, n: int) -> list[str]:
    """Register *n* users with deterministic emails; return the list."""
    emails = [f"user{i:02d}@example.com" for i in range(n)]
    for email in emails:
        _register(client, email=email)
    return emails


def _admin_login(app: FastAPI, client: TestClient, *, email: str = "ops@example.com") -> None:
    """Register + promote + HTML-login an admin user."""
    _register(client, email=email)
    _promote_admin_via_app(app, email=email)
    client.cookies.clear()
    _login_html(client, email=email)


def test_admin_users_default_page_1_size_20(client: TestClient, app: FastAPI) -> None:
    """No query params ⇒ page 1, size 20 ⇒ 20 rows in the table body."""
    _admin_login(app, client)
    _register_n_users(client, n=25)

    response = client.get("/admin/users", headers={"Accept": "text/html"}, follow_redirects=False)

    assert response.status_code == 200
    body = response.text
    assert "<tbody" in body
    # Count data rows by counting <tr ... > tags inside the body.
    # The header row counts as one; data rows = total <tr minus header.
    rows = body.count("<tr")
    assert rows == 21  # 20 data rows + 1 header row


def test_admin_users_renders_paginated_table(client: TestClient, app: FastAPI) -> None:
    """Page 2 / size 10 returns the second 10 users, plus prev/next links."""
    _admin_login(app, client)
    _register_n_users(client, n=25)

    response = client.get(
        "/admin/users",
        params={"page": 2, "size": 10},
        headers={"Accept": "text/html"},
    )

    assert response.status_code == 200
    body = response.text
    # 10 data rows + 1 header row.
    assert body.count("<tr") == 11
    # Prev / Next links must be present.
    assert "Prev" in body or "Previous" in body
    assert "Next" in body


def test_admin_users_html_escapes_email(client: TestClient, app: FastAPI) -> None:
    """A user with a ``<`` in their email renders escaped in the page."""
    weird_email = "weird<x>@example.io"
    _admin_login(app, client)
    _register(client, email=weird_email)

    response = client.get("/admin/users", headers={"Accept": "text/html"}, follow_redirects=False)

    assert response.status_code == 200
    body = response.text
    # The raw ``<x>`` must NEVER appear unescaped. The escaped form
    # (``&lt;x&gt;``) MUST appear instead.
    assert "weird<x>" not in body
    assert "weird&lt;x&gt;" in body


def test_admin_users_requires_admin_cookie(client: TestClient, app: FastAPI) -> None:
    """A visitor with no session is bounced to login (no API path)."""
    _register_n_users(client, n=3)

    response = client.get("/admin/users", headers={"Accept": "text/html"}, follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == "/auth/login?next=/admin/users"


# ---------------------------------------------------------------------------
# Helpers exposed to other test modules (kept here so the file is self-contained).
# ---------------------------------------------------------------------------


__all__ = [
    "SESSION_COOKIE_NAME",
    "_admin_login",
    "_login_html",
    "_promote_admin",
    "_promote_admin_via_app",
    "_register",
    "_register_n_users",
]
