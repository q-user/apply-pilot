"""Integration tests for the /search-profiles HTTP endpoints.

These tests use the real FastAPI app with a sqlite in-memory engine so
the route handlers, dependency injection, and DB session lifecycle are
exercised end-to-end. Authentication is performed via the /auth/* endpoints.
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
from apply_pilot.features.search_profiles import models as _sp_models  # noqa: F401
from apply_pilot.features.search_profiles.api import router as sp_router
from apply_pilot.features.users import models as _users_models  # noqa: F401
from apply_pilot.features.users.api import router as auth_router


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
    return _register_and_login(client, "sp-user@example.com", "hunter2!!")


# ---------------------------------------------------------------------------
# POST /search-profiles
# ---------------------------------------------------------------------------


def test_create_profile_returns_201(token: str, client: TestClient) -> None:
    """Creating a profile with a valid token must return 201 and the profile."""
    response = client.post(
        "/search-profiles",
        json={"title": "Python backend", "keywords": "django fastapi", "salary_min": 50000},
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 201
    body = response.json()
    assert body["title"] == "Python backend"
    assert body["keywords"] == "django fastapi"
    assert body["salary_min"] == 50000
    assert body["is_active"] is True
    assert body["id"]


def test_create_profile_without_token_returns_401(client: TestClient) -> None:
    """POST without a bearer token must return 401."""
    response = client.post("/search-profiles", json={"title": "x"})

    assert response.status_code == 401


def test_create_profile_with_invalid_token_returns_401(client: TestClient) -> None:
    """POST with a garbage token must return 401."""
    response = client.post(
        "/search-profiles",
        json={"title": "x"},
        headers={"Authorization": "Bearer not-a-real-token"},
    )

    assert response.status_code == 401


def test_create_profile_without_title_returns_422(token: str, client: TestClient) -> None:
    """A missing required field (title) must return 422."""
    response = client.post(
        "/search-profiles",
        json={"keywords": "python"},
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 422


# ---------------------------------------------------------------------------
# GET /search-profiles
# ---------------------------------------------------------------------------


def test_list_profiles_returns_only_own(token: str, client: TestClient) -> None:
    """Listing must only return profiles belonging to the authenticated user."""
    # Create two profiles for this user
    client.post(
        "/search-profiles",
        json={"title": "profile-1"},
        headers={"Authorization": f"Bearer {token}"},
    )
    client.post(
        "/search-profiles",
        json={"title": "profile-2"},
        headers={"Authorization": f"Bearer {token}"},
    )
    # Register another user and create a profile for them
    other_token = _register_and_login(client, "other@example.com", "hunter2!!")
    client.post(
        "/search-profiles",
        json={"title": "other-profile"},
        headers={"Authorization": f"Bearer {other_token}"},
    )

    response = client.get("/search-profiles", headers={"Authorization": f"Bearer {token}"})

    assert response.status_code == 200
    body = response.json()
    assert isinstance(body, list)
    titles = {p["title"] for p in body}
    assert titles == {"profile-1", "profile-2"}


def test_list_profiles_empty(token: str, client: TestClient) -> None:
    """A new user must get an empty list."""
    response = client.get("/search-profiles", headers={"Authorization": f"Bearer {token}"})

    assert response.status_code == 200
    assert response.json() == []


def test_list_profiles_without_token_returns_401(client: TestClient) -> None:
    """GET without a token must return 401."""
    response = client.get("/search-profiles")

    assert response.status_code == 401


# ---------------------------------------------------------------------------
# GET /search-profiles/{id}
# ---------------------------------------------------------------------------


def test_get_profile_returns_200(token: str, client: TestClient) -> None:
    """Getting a profile by id must return 200 and the profile."""
    created = client.post(
        "/search-profiles",
        json={"title": "my profile"},
        headers={"Authorization": f"Bearer {token}"},
    )
    profile_id = created.json()["id"]

    response = client.get(
        f"/search-profiles/{profile_id}",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 200
    assert response.json()["id"] == profile_id
    assert response.json()["title"] == "my profile"


def test_get_profile_of_other_user_returns_403(token: str, client: TestClient) -> None:
    """A user must not be able to read another user's profile."""
    other_token = _register_and_login(client, "victim@example.com", "hunter2!!")
    created = client.post(
        "/search-profiles",
        json={"title": "private"},
        headers={"Authorization": f"Bearer {other_token}"},
    )
    profile_id = created.json()["id"]

    response = client.get(
        f"/search-profiles/{profile_id}",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 403


def test_get_unknown_profile_returns_404(token: str, client: TestClient) -> None:
    """Requesting a non-existent id must return 404."""
    response = client.get(
        "/search-profiles/00000000-0000-0000-0000-000000000000",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 404


def test_get_profile_without_token_returns_401(client: TestClient) -> None:
    """GET without a token must return 401."""
    response = client.get("/search-profiles/00000000-0000-0000-0000-000000000000")

    assert response.status_code == 401


# ---------------------------------------------------------------------------
# PUT /search-profiles/{id}
# ---------------------------------------------------------------------------


def test_update_profile_returns_200(token: str, client: TestClient) -> None:
    """Updating a profile must return 200 with the updated fields."""
    created = client.post(
        "/search-profiles",
        json={"title": "old title", "keywords": "python"},
        headers={"Authorization": f"Bearer {token}"},
    )
    profile_id = created.json()["id"]

    response = client.put(
        f"/search-profiles/{profile_id}",
        json={"title": "new title"},
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["title"] == "new title"
    assert body["keywords"] == "python"  # unchanged


def test_update_other_user_profile_returns_403(token: str, client: TestClient) -> None:
    """A user must not be able to update another user's profile."""
    other_token = _register_and_login(client, "target@example.com", "hunter2!!")
    created = client.post(
        "/search-profiles",
        json={"title": "private"},
        headers={"Authorization": f"Bearer {other_token}"},
    )
    profile_id = created.json()["id"]

    response = client.put(
        f"/search-profiles/{profile_id}",
        json={"title": "hacked"},
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 403


def test_update_unknown_profile_returns_404(token: str, client: TestClient) -> None:
    """Updating a non-existent profile must return 404."""
    response = client.put(
        "/search-profiles/00000000-0000-0000-0000-000000000000",
        json={"title": "x"},
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 404


def test_update_profile_without_token_returns_401(client: TestClient) -> None:
    """PUT without a token must return 401."""
    response = client.put(
        "/search-profiles/00000000-0000-0000-0000-000000000000",
        json={"title": "x"},
    )

    assert response.status_code == 401


def test_update_invalid_salary_range_returns_422(token: str, client: TestClient) -> None:
    """Updating salary_min above salary_max must return 422."""
    created = client.post(
        "/search-profiles",
        json={"title": "salary-test", "salary_min": 30000, "salary_max": 50000},
        headers={"Authorization": f"Bearer {token}"},
    )
    profile_id = created.json()["id"]

    response = client.put(
        f"/search-profiles/{profile_id}",
        json={"salary_min": 100000},
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 422


# ---------------------------------------------------------------------------
# DELETE /search-profiles/{id}
# ---------------------------------------------------------------------------


def test_delete_profile_returns_204(token: str, client: TestClient) -> None:
    """Deleting a profile must return 204 and remove the profile."""
    created = client.post(
        "/search-profiles",
        json={"title": "to-delete"},
        headers={"Authorization": f"Bearer {token}"},
    )
    profile_id = created.json()["id"]

    delete_resp = client.delete(
        f"/search-profiles/{profile_id}",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert delete_resp.status_code == 204

    get_resp = client.get(
        f"/search-profiles/{profile_id}",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert get_resp.status_code == 404


def test_delete_other_user_profile_returns_403(token: str, client: TestClient) -> None:
    """A user must not be able to delete another user's profile."""
    other_token = _register_and_login(client, "delete-target@example.com", "hunter2!!")
    created = client.post(
        "/search-profiles",
        json={"title": "private"},
        headers={"Authorization": f"Bearer {other_token}"},
    )
    profile_id = created.json()["id"]

    response = client.delete(
        f"/search-profiles/{profile_id}",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 403


def test_delete_unknown_profile_returns_404(token: str, client: TestClient) -> None:
    """Deleting a non-existent profile must return 404."""
    response = client.delete(
        "/search-profiles/00000000-0000-0000-0000-000000000000",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 404


def test_delete_profile_without_token_returns_401(client: TestClient) -> None:
    """DELETE without a token must return 401."""
    response = client.delete("/search-profiles/00000000-0000-0000-0000-000000000000")

    assert response.status_code == 401
