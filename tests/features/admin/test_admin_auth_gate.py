"""TDD tests for the tightened admin auth gate (M6, issue #171).

Before this change, :func:`apply_pilot.features.admin._auth.require_admin_user`
resolved any valid bearer token to its user id and let the request
through. After this change, it must also look up the user record and
check the new ``is_admin`` flag — non-admin users get ``403``, not
``200``.

These tests describe the new gate end-to-end:

* Valid token + ``is_admin=True``   ⇒ request succeeds (200/2xx).
* Valid token + ``is_admin=False``  ⇒ request fails with 403.
* Missing token                     ⇒ request fails with 401 (unchanged).

The cookie fallback is also exercised: a session cookie alone must
satisfy the gate exactly the same way the bearer header does. This
mirrors the cookie-aware behaviour that PR #170 introduced for the
``/auth/*`` routes.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from contextlib import suppress

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import StaticPool, create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from apply_pilot.app import create_app
from apply_pilot.config import get_admin_auth_required
from apply_pilot.db import Base, get_db
from apply_pilot.features.admin.health import (
    HealthCheckResult,
)
from apply_pilot.features.users import models as _users_models  # noqa: F401
from apply_pilot.features.users.security import issue_token
from apply_pilot.features.users.session import SESSION_COOKIE_NAME

# ---------------------------------------------------------------------------
# Fixtures
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
def app(engine: Engine) -> Iterator[FastAPI]:
    """Build the production FastAPI app with an in-memory sqlite engine.

    The :func:`get_admin_auth_required` flag is flipped to ``True`` so
    the new gate (token + ``is_admin``) is exercised exactly as it
    will be in production.
    """
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
    try:
        yield application
    finally:
        application.dependency_overrides.clear()


@pytest.fixture
def client(app: FastAPI) -> Iterator[TestClient]:
    with TestClient(app) as c:
        yield c


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _register(client: TestClient, *, email: str, password: str = "hunter2!!") -> None:
    """Helper: register a user via the JSON API."""
    response = client.post("/auth/register", json={"email": email, "password": password})
    assert response.status_code == 201, response.text


def _promote(*, email: str, is_admin: bool, session: Session) -> None:
    """Flip ``is_admin`` on the user with *email* directly in the DB."""
    from apply_pilot.features.users.models import User

    user = session.query(User).filter_by(email=email).one()
    user.is_admin = is_admin
    session.commit()


def _admin_token_for(email: str, *, is_admin: bool, app: FastAPI) -> str:
    """Register + promote + issue a token for *email*."""
    # ``client`` is the live TestClient; we don't have it here, so we
    # use the DB to set up the user record directly. The token still
    # resolves through the in-memory token store because we go through
    # the regular ``/auth/login`` JSON path in the tests below.
    from apply_pilot.features.users.models import User
    from apply_pilot.features.users.security import hash_password

    # Insert directly — we don't want to depend on the HTTP layer here.
    get_db_override = app.dependency_overrides[get_db]
    gen = get_db_override()
    session = next(iter(gen))
    try:
        user = User(
            id=uuid.uuid4(),
            email=email.lower(),
            hashed_password=hash_password("hunter2!!"),
            is_active=True,
            is_admin=is_admin,
        )
        session.add(user)
        session.commit()
        # ``expire_on_commit`` defaults to True, so capturing the id
        # before the session closes avoids a lazy-refresh on a detached
        # instance below.
        user_id = user.id
    finally:
        with suppress(StopIteration):
            next(gen)
    return issue_token(str(user_id), ttl_seconds=300)


# ---------------------------------------------------------------------------
# Tests — bearer header path
# ---------------------------------------------------------------------------


def test_require_admin_user_rejects_non_admin(client: TestClient, app: FastAPI) -> None:
    """A valid bearer token belonging to a non-admin user must yield 403."""
    token = _admin_token_for("alice@example.com", is_admin=False, app=app)

    response = client.get(
        "/admin/integrations",
        headers={"Authorization": f"Bearer {token}"},
    )

    # ``require_admin_user`` must reject the request with 403, not
    # let it through to the handler (which would have returned 200).
    assert response.status_code == 403, response.text


def test_require_admin_user_accepts_admin(client: TestClient, app: FastAPI) -> None:
    """A valid bearer token belonging to an admin user must succeed."""
    token = _admin_token_for("ops@example.com", is_admin=True, app=app)

    response = client.get(
        "/admin/integrations",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 200, response.text


def test_require_admin_user_rejects_anonymous(client: TestClient) -> None:
    """No credential at all still yields 401 (unchanged)."""
    response = client.get("/admin/integrations")
    assert response.status_code == 401
    assert response.json()["detail"]["code"] == "authentication_required"


# ---------------------------------------------------------------------------
# Tests — cookie fallback path
# ---------------------------------------------------------------------------


def test_require_admin_user_cookie_fallback_for_admin(client: TestClient, app: FastAPI) -> None:
    """A session cookie alone (no Authorization header) satisfies the gate."""
    token = _admin_token_for("ops@example.com", is_admin=True, app=app)

    response = client.get(
        "/admin/integrations",
        cookies={SESSION_COOKIE_NAME: token},
    )

    assert response.status_code == 200, response.text


def test_require_admin_user_cookie_rejects_non_admin(client: TestClient, app: FastAPI) -> None:
    """A session cookie for a non-admin user must also yield 403."""
    token = _admin_token_for("alice@example.com", is_admin=False, app=app)

    response = client.get(
        "/admin/integrations",
        cookies={SESSION_COOKIE_NAME: token},
    )

    assert response.status_code == 403, response.text


def test_require_admin_user_rejects_invalid_token(client: TestClient) -> None:
    """A garbage Authorization header still yields 401 ``invalid_token``."""
    response = client.get(
        "/admin/integrations",
        headers={"Authorization": "Bearer not-a-real-token"},
    )
    assert response.status_code == 401
    assert response.json()["detail"]["code"] == "invalid_token"


# ---------------------------------------------------------------------------
# Tests — JSON admin endpoint smoke
# ---------------------------------------------------------------------------


def test_existing_admin_health_returns_200_for_admin(client: TestClient, app: FastAPI) -> None:
    """The pre-existing ``/admin/health`` page still renders for admins."""
    # Stub the health checks so the page doesn't try to talk to real infra.
    from apply_pilot.features.admin.health import (
        HealthCheckResult,
        HealthStatus,
        get_health_checks,
    )

    application = app

    def _override_checks() -> list:
        return [
            _StubHealthCheck(
                "database",
                HealthCheckResult(
                    name="database",
                    status=HealthStatus.HEALTHY,
                    detail="ok",
                ),
            ),
        ]

    application.dependency_overrides[get_health_checks] = _override_checks

    token = _admin_token_for("ops@example.com", is_admin=True, app=application)

    response = client.get(
        "/admin/health",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 200, response.text


# ---------------------------------------------------------------------------
# Inline stub for the health-check dependency.
# ---------------------------------------------------------------------------


class _StubHealthCheck:
    """Minimal :class:`HealthCheck` stub used only by these tests."""

    def __init__(self, name: str, result: HealthCheckResult) -> None:
        self._name = name
        self._result = result

    @property
    def name(self) -> str:
        return self._name

    async def run(self) -> HealthCheckResult:
        return self._result
