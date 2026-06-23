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

from apply_pilot.db import Base, get_db
from apply_pilot.features.matches import models as _matches_models  # noqa: F401
from apply_pilot.features.matches.api import router as matches_router
from apply_pilot.features.search_profiles import models as _sp_models  # noqa: F401
from apply_pilot.features.search_profiles.api import router as sp_router
from apply_pilot.features.search_profiles.models import SearchProfile
from apply_pilot.features.sources import models as _sources_models  # noqa: F401
from apply_pilot.features.sources.models import Vacancy
from apply_pilot.features.users import models as _users_models  # noqa: F401
from apply_pilot.features.users.api import router as auth_router
from apply_pilot.features.users.models import User
from apply_pilot.features.users.security import hash_password


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
    from apply_pilot.features.matches.models import VacancyMatch
    from apply_pilot.features.matches.repository import SqlVacancyMatchRepository

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

    from apply_pilot.features.matches.models import VacancyMatch
    from apply_pilot.features.matches.repository import SqlVacancyMatchRepository

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

    from apply_pilot.features.matches.models import VacancyMatch
    from apply_pilot.features.matches.repository import SqlVacancyMatchRepository

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

    from apply_pilot.features.matches.models import VacancyMatch
    from apply_pilot.features.matches.repository import SqlVacancyMatchRepository

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

    from apply_pilot.features.matches.models import VacancyMatch
    from apply_pilot.features.matches.repository import SqlVacancyMatchRepository

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

    from apply_pilot.features.matches.models import VacancyMatch
    from apply_pilot.features.matches.repository import SqlVacancyMatchRepository

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

    from apply_pilot.features.matches.models import VacancyMatch
    from apply_pilot.features.matches.repository import SqlVacancyMatchRepository

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

    from apply_pilot.features.matches.models import VacancyMatch
    from apply_pilot.features.matches.repository import SqlVacancyMatchRepository

    match = SqlVacancyMatchRepository(session_factory=session_factory).create(
        VacancyMatch(search_profile_id=uuid.UUID(profile), vacancy_id=vacancy)
    )

    response = client.patch(
        f"/matches/{match.id}/status",
        json={"status": "bogus"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 422


# ---------------------------------------------------------------------------
# Regression test for issue #139
# ---------------------------------------------------------------------------
#
# ``get_match_service`` builds a :class:`MatchService` whose two SQL repos
# share the FastAPI request session. The OLD code passed
# ``session_factory=lambda: session`` and the SQL repos called
# ``session.close()`` in their ``finally`` block, which closed the request
# session mid-request. The second repo (the ownership-check
# :class:`SqlSearchProfileRepository`) then operated on a closed session.
# On a real database the second call would raise
# :class:`DetachedInstanceError` or :class:`OperationalError`; on the
# in-memory sqlite used here SQLAlchemy silently re-opens the session, so
# the only reliable signal is the ``close()`` count on the request session.
#
# With the new ``session=session`` wiring the repos do **not** close the
# caller-supplied session, so the request session is closed exactly once:
# by the ``get_db`` teardown. With the OLD code the count is three
# (two repo calls plus the teardown).


class _CountingSession(Session):
    """SQLAlchemy ``Session`` that records how many times ``close`` runs.

    The fixture installs a ``session_factory`` returning this subclass
    only for the request session. The seeder continues to use the plain
    SQLAlchemy ``Session`` so the counts reflect exclusively the
    lifetime of the FastAPI request.
    """

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.close_count = 0

    def close(self) -> None:  # type: ignore[override]
        self.close_count += 1
        super().close()


def test_get_match_does_not_close_request_session_mid_request(
    engine: Engine,
) -> None:
    """``GET /matches/{id}`` must not close the request session mid-request.

    Issue #139: the SQL repos wired into ``get_match_service`` previously
    closed the request session (passed via ``session_factory=lambda``)
    after every operation, so the second repo call inside the same
    request ran on a closed session. The fix binds the request session
    once via ``session=session`` and skips the close. This test pins the
    behaviour: the request session must be closed exactly once, by
    ``get_db``'s teardown, no matter how many repos the handler
    touches.
    """
    plain_session_factory = sessionmaker(
        bind=engine, class_=Session, autocommit=False, autoflush=False
    )
    counting_session_factory = sessionmaker(
        bind=engine, class_=_CountingSession, autocommit=False, autoflush=False
    )

    # ---- Pre-seed: a user, a profile, a vacancy, and a match. The seeder
    # uses the *plain* sessionmaker so the counts we later assert only
    # reflect what happens during the FastAPI request.
    user_id = uuid.uuid4()
    profile_id = uuid.uuid4()
    vacancy_id = uuid.uuid4()
    match_id = uuid.uuid4()

    with plain_session_factory() as session:
        session.add(
            User(
                id=user_id,
                email="session-test@example.com",
                hashed_password=hash_password("hunter2!!"),
            )
        )
        session.add(
            SearchProfile(
                id=profile_id,
                user_id=user_id,
                title="session-test",
            )
        )
        session.add(
            Vacancy(
                id=vacancy_id,
                source="hh",
                source_id="hh-1",
                title="Python Dev",
                raw_data={"id": "hh-1", "name": "Python Dev"},
            )
        )
        from apply_pilot.features.matches.models import VacancyMatch
        from apply_pilot.features.matches.repository import SqlVacancyMatchRepository

        # Commit the parent rows so the match insert below can resolve
        # the foreign keys (and the endpoint's request session can see them).
        session.commit()
        SqlVacancyMatchRepository(session_factory=plain_session_factory).create(
            VacancyMatch(id=match_id, search_profile_id=profile_id, vacancy_id=vacancy_id)
        )

    # ---- Build a FastAPI app whose ``get_db`` returns the counting
    # session. We capture each yielded session in a list so the test
    # can inspect its ``close_count`` after the request.
    request_sessions: list[_CountingSession] = []

    def _override_get_db() -> Iterator[_CountingSession]:
        session = counting_session_factory()
        request_sessions.append(session)
        try:
            yield session
        finally:
            session.close()

    application = FastAPI()
    application.include_router(matches_router)
    application.include_router(sp_router)
    application.include_router(auth_router)
    application.dependency_overrides[get_db] = _override_get_db

    # The auth router resolves the bearer token by user id; the seeded
    # user was created with the same id we pass through the token store.
    from apply_pilot.features.users.security import default_token_store

    token = default_token_store().issue(str(user_id), ttl_seconds=3600)

    with TestClient(application) as client:
        # Sanity: register / login are not exercised because the test
        # directly mints a token for the seeded user. The endpoint under
        # test (``GET /matches/{id}``) goes through
        # ``get_match_service``, which is where the bug lived.
        response = client.get(f"/matches/{match_id}", headers={"Authorization": f"Bearer {token}"})

    application.dependency_overrides.clear()

    # The response is well-formed and contains the seeded match. With
    # the OLD code ``get_match_service``'s repo calls would close the
    # request session mid-request, so the ownership check
    # (``_profile_repo.get_by_id``) would operate on a closed session.
    # Even when the second call appears to succeed against an
    # in-memory sqlite, the close count diverges.
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["id"] == str(match_id)
    assert body["search_profile_id"] == str(profile_id)
    assert body["vacancy_id"] == str(vacancy_id)

    # The pinning assertion: the request session must be closed exactly
    # once. With the OLD ``session_factory=lambda: session`` wiring each
    # SQL repo call closed the request session in its ``finally`` block,
    # so the count would be 1 (get_db teardown) + N (one per repo call).
    # ``GET /matches/{id}`` touches two repos (match + profile), so the
    # OLD count is 3; the NEW count is 1.
    assert len(request_sessions) == 1, (
        "expected exactly one request session to be observed by get_db"
    )
    request_session = request_sessions[0]
    assert request_session.close_count == 1, (
        "request session was closed "
        f"{request_session.close_count} times during GET /matches/{match_id}; "
        "expected exactly 1 (the get_db teardown). A higher count means the "
        "SQL repos wired into get_match_service still close the request "
        "session mid-request — issue #139 has regressed."
    )
