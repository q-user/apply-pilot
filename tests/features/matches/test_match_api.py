"""Integration tests for the /matches HTTP endpoints.

These tests use the real FastAPI app with a sqlite in-memory engine so
the route handlers, dependency injection, DB session lifecycle, and
bearer-token authentication are exercised end-to-end.
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

from job_apply.db import Base, get_db
from job_apply.features.matches import models as _matches_models  # noqa: F401
from job_apply.features.matches.api import router as matches_router
from job_apply.features.search_profiles import models as _sp_models  # noqa: F401
from job_apply.features.search_profiles.api import router as sp_router
from job_apply.features.sources import models as _sources_models  # noqa: F401
from job_apply.features.sources.models import Vacancy
from job_apply.features.users import models as _users_models  # noqa: F401
from job_apply.features.users.api import router as auth_router
from job_apply.features.users.models import User
from job_apply.features.users.security import hash_password


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
def session_factory(engine: Engine):
    return sessionmaker(bind=engine, class_=Session, autocommit=False, autoflush=False)


@pytest.fixture
def app(session_factory) -> Iterator[FastAPI]:
    def _override_get_db() -> Iterator[Session]:
        session = session_factory()
        try:
            yield session
        finally:
            session.close()

    application = FastAPI()
    application.include_router(auth_router)
    application.include_router(sp_router)
    application.include_router(matches_router)
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
    return _register_and_login(client, "match-user@example.com", "hunter2!!")


@pytest.fixture
def other_token(client: TestClient) -> str:
    return _register_and_login(client, "other-match-user@example.com", "hunter2!!")


def _seed_vacancy(session_factory) -> uuid.UUID:
    """Persist a Vacancy and return its id."""
    session = session_factory()
    try:
        vacancy = Vacancy(
            id=uuid.uuid4(),
            source="hh",
            source_id="hh-1",
            title="Python Dev",
            raw_data={"id": "hh-1", "name": "Python Dev"},
        )
        session.add(vacancy)
        session.commit()
        return vacancy.id
    finally:
        session.close()


def _seed_user(session_factory, *, email: str) -> uuid.UUID:
    """Persist a user row and return the id (bypasses /auth for setup)."""
    session = session_factory()
    try:
        user = User(id=uuid.uuid4(), email=email, hashed_password=hash_password("pw"))
        session.add(user)
        session.commit()
        return user.id
    finally:
        session.close()


def _create_profile_via_api(client: TestClient, token: str, title: str) -> str:
    response = client.post(
        "/search-profiles",
        json={"title": title},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 201, response.json()
    return response.json()["id"]


# ---------------------------------------------------------------------------
# GET /matches
# ---------------------------------------------------------------------------


def test_list_matches_requires_token(client: TestClient) -> None:
    """GET /matches without a bearer token must return 401."""
    response = client.get("/matches")
    assert response.status_code == 401


def test_list_matches_empty_for_new_user(token: str, client: TestClient) -> None:
    """A user with no matches must get an empty list."""
    response = client.get("/matches", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 200
    assert response.json() == []


def test_list_matches_returns_only_own(
    token: str,
    other_token: str,
    client: TestClient,
    session_factory,
) -> None:
    """Only the caller's matches must appear in the listing."""
    my_profile = _create_profile_via_api(client, token, "mine")
    their_profile = _create_profile_via_api(client, other_token, "theirs")
    vacancy = _seed_vacancy(session_factory)

    # Seed matches directly through the SQL repos so the test isolates the
    # list endpoint from the create / bulk paths.
    from job_apply.features.matches.models import VacancyMatch
    from job_apply.features.matches.repository import SqlVacancyMatchRepository

    repo = SqlVacancyMatchRepository(session_factory=session_factory)
    repo.create(VacancyMatch(search_profile_id=uuid.UUID(my_profile), vacancy_id=vacancy))
    repo.create(VacancyMatch(search_profile_id=uuid.UUID(their_profile), vacancy_id=vacancy))

    response = client.get("/matches", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 200
    body = response.json()
    assert len(body) == 1
    assert body[0]["search_profile_id"] == my_profile


def test_list_matches_filters_by_status(token: str, client: TestClient, session_factory) -> None:
    """A ?status= query must restrict the listing to that status."""
    profile = _create_profile_via_api(client, token, "p")
    vacancy = _seed_vacancy(session_factory)

    from job_apply.features.matches.models import VacancyMatch
    from job_apply.features.matches.repository import SqlVacancyMatchRepository

    repo = SqlVacancyMatchRepository(session_factory=session_factory)
    repo.create(VacancyMatch(search_profile_id=uuid.UUID(profile), vacancy_id=vacancy))

    response = client.get("/matches?status=new", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 200
    body = response.json()
    assert len(body) == 1

    response = client.get("/matches?status=accepted", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 200
    assert response.json() == []


def test_list_matches_rejects_unknown_status(token: str, client: TestClient) -> None:
    """An unknown ?status= must return 422."""
    response = client.get("/matches?status=bogus", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 422


# ---------------------------------------------------------------------------
# GET /matches/{id}
# ---------------------------------------------------------------------------


def test_get_match_returns_200(token: str, client: TestClient, session_factory) -> None:
    """Getting an owned match must return 200 with the match."""
    profile = _create_profile_via_api(client, token, "p")
    vacancy = _seed_vacancy(session_factory)

    from job_apply.features.matches.models import VacancyMatch
    from job_apply.features.matches.repository import SqlVacancyMatchRepository

    match = SqlVacancyMatchRepository(session_factory=session_factory).create(
        VacancyMatch(search_profile_id=uuid.UUID(profile), vacancy_id=vacancy)
    )

    response = client.get(f"/matches/{match.id}", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 200
    assert response.json()["id"] == str(match.id)


def test_get_match_of_other_user_returns_403(
    token: str,
    other_token: str,
    client: TestClient,
    session_factory,
) -> None:
    """A user must not be able to read another user's match."""
    profile = _create_profile_via_api(client, other_token, "p")
    vacancy = _seed_vacancy(session_factory)

    from job_apply.features.matches.models import VacancyMatch
    from job_apply.features.matches.repository import SqlVacancyMatchRepository

    match = SqlVacancyMatchRepository(session_factory=session_factory).create(
        VacancyMatch(search_profile_id=uuid.UUID(profile), vacancy_id=vacancy)
    )

    response = client.get(f"/matches/{match.id}", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 403


def test_get_match_unknown_id_returns_404(token: str, client: TestClient) -> None:
    """A non-existent match must return 404."""
    response = client.get(f"/matches/{uuid.uuid4()}", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 404


def test_get_match_invalid_id_returns_404(token: str, client: TestClient) -> None:
    """A malformed match id must return 404 (not 500)."""
    response = client.get("/matches/not-a-uuid", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 404


# ---------------------------------------------------------------------------
# PATCH /matches/{id}/status
# ---------------------------------------------------------------------------


def test_patch_match_status_returns_200(token: str, client: TestClient, session_factory) -> None:
    """Updating the status must return 200 with the new state."""
    profile = _create_profile_via_api(client, token, "p")
    vacancy = _seed_vacancy(session_factory)

    from job_apply.features.matches.models import VacancyMatch
    from job_apply.features.matches.repository import SqlVacancyMatchRepository

    match = SqlVacancyMatchRepository(session_factory=session_factory).create(
        VacancyMatch(search_profile_id=uuid.UUID(profile), vacancy_id=vacancy)
    )

    response = client.patch(
        f"/matches/{match.id}/status",
        json={"status": "accepted"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "accepted"


def test_patch_match_status_with_score_returns_200(
    token: str, client: TestClient, session_factory
) -> None:
    """Updating the status with a score must persist both."""
    profile = _create_profile_via_api(client, token, "p")
    vacancy = _seed_vacancy(session_factory)

    from job_apply.features.matches.models import VacancyMatch
    from job_apply.features.matches.repository import SqlVacancyMatchRepository

    match = SqlVacancyMatchRepository(session_factory=session_factory).create(
        VacancyMatch(search_profile_id=uuid.UUID(profile), vacancy_id=vacancy)
    )

    response = client.patch(
        f"/matches/{match.id}/status",
        json={"status": "scored", "score": 85},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "scored"
    assert body["score"] == 85


def test_patch_match_other_user_returns_403(
    token: str,
    other_token: str,
    client: TestClient,
    session_factory,
) -> None:
    """A user must not be able to mutate another user's match."""
    profile = _create_profile_via_api(client, other_token, "p")
    vacancy = _seed_vacancy(session_factory)

    from job_apply.features.matches.models import VacancyMatch
    from job_apply.features.matches.repository import SqlVacancyMatchRepository

    match = SqlVacancyMatchRepository(session_factory=session_factory).create(
        VacancyMatch(search_profile_id=uuid.UUID(profile), vacancy_id=vacancy)
    )

    response = client.patch(
        f"/matches/{match.id}/status",
        json={"status": "rejected"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 403


def test_patch_match_unknown_id_returns_404(token: str, client: TestClient) -> None:
    response = client.patch(
        f"/matches/{uuid.uuid4()}/status",
        json={"status": "accepted"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 404


def test_patch_match_invalid_status_returns_422(
    token: str, client: TestClient, session_factory
) -> None:
    """An unknown status value must return 422."""
    profile = _create_profile_via_api(client, token, "p")
    vacancy = _seed_vacancy(session_factory)

    from job_apply.features.matches.models import VacancyMatch
    from job_apply.features.matches.repository import SqlVacancyMatchRepository

    match = SqlVacancyMatchRepository(session_factory=session_factory).create(
        VacancyMatch(search_profile_id=uuid.UUID(profile), vacancy_id=vacancy)
    )

    response = client.patch(
        f"/matches/{match.id}/status",
        json={"status": "bogus"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 422
