"""TDD tests for the search profile activation / deactivation / preferred use cases.

These tests describe the behaviour the search_profiles slice must deliver
through both the service layer and the HTTP layer. The service tests use
the in-memory repository; the API tests use the real FastAPI app with a
sqlite in-memory engine so routing, dependencies, and JSON contracts are
exercised end-to-end.
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
from apply_pilot.features.search_profiles import models as _sp_models  # noqa: F401
from apply_pilot.features.search_profiles.api import router as sp_router
from apply_pilot.features.search_profiles.repository import InMemorySearchProfileRepository
from apply_pilot.features.search_profiles.schemas import SearchProfileCreate
from apply_pilot.features.search_profiles.service import (
    ProfileNotFoundError,
    ProfileOwnershipError,
    SearchProfileService,
)
from apply_pilot.features.users import models as _users_models  # noqa: F401
from apply_pilot.features.users.api import router as auth_router

# ---------------------------------------------------------------------------
# Service-level fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def repo() -> InMemorySearchProfileRepository:
    return InMemorySearchProfileRepository()


@pytest.fixture
def service(repo: InMemorySearchProfileRepository) -> SearchProfileService:
    return SearchProfileService(repo)


@pytest.fixture
def user_id() -> uuid.UUID:
    return uuid.uuid4()


@pytest.fixture
def other_user_id() -> uuid.UUID:
    return uuid.uuid4()


# ---------------------------------------------------------------------------
# Service-level: set_active
# ---------------------------------------------------------------------------


def test_activate_profile_sets_is_active_true(
    service: SearchProfileService, user_id: uuid.UUID
) -> None:
    """Calling ``set_active(..., active=True)`` must flip ``is_active`` to True."""
    created = service.create(SearchProfileCreate(title="dormant"), user_id=user_id)

    result = service.set_active(created.id, active=True, user_id=user_id)

    assert result.id == created.id
    assert result.is_active is True


def test_deactivate_profile_sets_is_active_false(
    service: SearchProfileService, user_id: uuid.UUID
) -> None:
    """Calling ``set_active(..., active=False)`` must flip ``is_active`` to False."""
    created = service.create(SearchProfileCreate(title="active"), user_id=user_id)

    result = service.set_active(created.id, active=False, user_id=user_id)

    assert result.id == created.id
    assert result.is_active is False


def test_activate_returns_updated_profile(
    service: SearchProfileService, repo: InMemorySearchProfileRepository, user_id: uuid.UUID
) -> None:
    """The DTO returned by ``set_active`` must reflect the new state, and the
    repository must persist the change (i.e. subsequent reads see the new flag)."""
    created = service.create(SearchProfileCreate(title="x"), user_id=user_id)

    result = service.set_active(created.id, active=True, user_id=user_id)

    assert result.is_active is True
    # Reload from the repository — the flag must be persisted.
    reloaded = repo.get_by_id(created.id)
    assert reloaded is not None
    assert reloaded.is_active is True


def test_activate_profile_for_non_owner_returns_403(
    service: SearchProfileService, user_id: uuid.UUID, other_user_id: uuid.UUID
) -> None:
    """A user must not be able to flip the active flag on another user's profile."""
    created = service.create(SearchProfileCreate(title="private"), user_id=user_id)

    with pytest.raises(ProfileOwnershipError):
        service.set_active(created.id, active=True, user_id=other_user_id)

    with pytest.raises(ProfileOwnershipError):
        service.set_active(created.id, active=False, user_id=other_user_id)


def test_activate_unknown_profile_raises_not_found(
    service: SearchProfileService, user_id: uuid.UUID
) -> None:
    """Toggling a non-existent profile must raise ``ProfileNotFoundError``."""
    with pytest.raises(ProfileNotFoundError):
        service.set_active(uuid.uuid4(), active=True, user_id=user_id)


# ---------------------------------------------------------------------------
# API-level: HTTP router wiring
# ---------------------------------------------------------------------------


def _register_and_login(client: TestClient, email: str, password: str) -> str:
    """Helper: register a user and return the access token."""
    resp = client.post("/auth/register", json={"email": email, "password": password})
    assert resp.status_code == 201, resp.json()
    resp = client.post("/auth/login", json={"email": email, "password": password})
    assert resp.status_code == 200, resp.json()
    return resp.json()["access_token"]


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
    application.include_router(sp_router)
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


@pytest.fixture
def token(client: TestClient) -> str:
    """Return a valid bearer token for a freshly registered user."""
    return _register_and_login(client, "activation-user@example.com", "hunter2!!")


def test_api_activate_endpoint(token: str, client: TestClient) -> None:
    """``POST /search-profiles/{id}/activate`` must return 200 with ``is_active=true``."""
    created = client.post(
        "/search-profiles",
        json={"title": "to-activate"},
        headers={"Authorization": f"Bearer {token}"},
    )
    profile_id = created.json()["id"]

    response = client.post(
        f"/search-profiles/{profile_id}/activate",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["id"] == profile_id
    assert body["is_active"] is True


def test_preferred_endpoint_returns_404_when_none(token: str, client: TestClient) -> None:
    """``GET /search-profiles/preferred`` must return 404 when the user has no
    preferred profile (the default state — this endpoint is a placeholder
    for a future "set preferred profile" feature)."""
    response = client.get(
        "/search-profiles/preferred",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 404
