"""End-to-end tests for the /admin/scoring-review HTTP endpoints (M8, issue #68).

The slice is read-mostly: ``GET /admin/scoring-review/queue`` lists the
matches with low LLM confidence and ``POST /admin/scoring-review/{match_id}/note``
records a reviewer note. The endpoints do not require a bearer token
(per the M6 admin-slice contract); operators access them directly.

The tests stand up a real FastAPI app with a sqlite in-memory engine,
seed profiles / vacancies / matches through the SQL repos, and assert
the wire format returned to the caller.
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
from apply_pilot.features.audit import models as _audit_models  # noqa: F401
from apply_pilot.features.matches import models as _matches_models  # noqa: F401
from apply_pilot.features.matches.models import MatchStatus, VacancyMatch
from apply_pilot.features.scoring_review.api import router as scoring_review_router
from apply_pilot.features.search_profiles import models as _sp_models  # noqa: F401
from apply_pilot.features.search_profiles.models import SearchProfile
from apply_pilot.features.sources import models as _sources_models  # noqa: F401
from apply_pilot.features.sources.models import Vacancy
from apply_pilot.features.users import models as _users_models  # noqa: F401
from apply_pilot.features.users.models import User


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
    application.include_router(scoring_review_router)
    application.dependency_overrides[get_db] = _override_get_db
    try:
        yield application
    finally:
        application.dependency_overrides.clear()


@pytest.fixture
def client(app: FastAPI) -> Iterator[TestClient]:
    with TestClient(app) as c:
        yield c


def _seed(session_factory, *, confidence: float | None) -> uuid.UUID:
    """Insert a user / profile / vacancy / match and return the match id."""
    session = session_factory()
    try:
        user = User(
            id=uuid.uuid4(), email=f"u{uuid.uuid4().hex[:8]}@example.com", hashed_password="x"
        )
        session.add(user)
        session.flush()
        profile = SearchProfile(id=uuid.uuid4(), user_id=user.id, title="p", is_active=True)
        session.add(profile)
        session.flush()
        vacancy = Vacancy(
            id=uuid.uuid4(),
            source="hh",
            source_id=f"hh-{uuid.uuid4().hex[:6]}",
            title="t",
            raw_data={},
        )
        session.add(vacancy)
        session.flush()
        match = VacancyMatch(
            id=uuid.uuid4(),
            search_profile_id=profile.id,
            vacancy_id=vacancy.id,
            status=MatchStatus.SCORED.value,
        )
        match.score = 10
        match.confidence = confidence
        match.prompt_version = "vacancy_scoring@v1"
        match.explanation = "low confidence"
        session.add(match)
        session.commit()
        return match.id
    finally:
        session.close()


def test_queue_default_threshold_orders_ascending(client: TestClient, session_factory) -> None:
    """``GET /admin/scoring-review/queue`` must default to threshold=0.5."""
    _seed(session_factory, confidence=0.1)
    _seed(session_factory, confidence=0.4)
    _seed(session_factory, confidence=0.9)  # excluded

    response = client.get("/admin/scoring-review/queue")
    assert response.status_code == 200
    body = response.json()
    confidences = [row["confidence"] for row in body]
    assert confidences == [0.1, 0.4]
    for row in body:
        assert row["prompt_version"] == "vacancy_scoring@v1"
        assert row["explanation"] == "low confidence"
        # Every row carries the user and profile ids so the admin can drill
        # down without a second round-trip.
        assert "user_id" in row
        assert "search_profile_id" in row


def test_queue_respects_threshold_and_limit(client: TestClient, session_factory) -> None:
    """``?threshold=`` and ``?limit=`` must be honoured."""
    for _ in range(5):
        _seed(session_factory, confidence=0.1)
    _seed(session_factory, confidence=0.8)

    response = client.get("/admin/scoring-review/queue?threshold=0.5&limit=3")
    assert response.status_code == 200
    assert len(response.json()) == 3

    # Threshold=0.05 means only confidence strictly below 0.05 returns; the
    # seeded rows (confidence=0.1) are excluded.
    response = client.get("/admin/scoring-review/queue?threshold=0.05")
    assert response.status_code == 200
    assert response.json() == []


def test_queue_rejects_invalid_threshold(client: TestClient) -> None:
    """``threshold`` outside [0, 1] must return 422."""
    response = client.get("/admin/scoring-review/queue?threshold=2.0")
    assert response.status_code == 422


def test_note_endpoint_writes_audit_event(
    client: TestClient, session_factory, engine: Engine
) -> None:
    """``POST /admin/scoring-review/{id}/note`` must persist a MATCH_REVIEWED row."""
    match_id = _seed(session_factory, confidence=0.1)

    response = client.post(
        f"/admin/scoring-review/{match_id}/note",
        json={"note": "looks fine, but low confidence due to sparse JD"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["match_id"] == str(match_id)
    assert body["note"] == "looks fine, but low confidence due to sparse JD"
    assert body["event_type"] == "match_reviewed"

    # Verify the audit log row landed in the DB.
    from sqlalchemy import text

    with engine.connect() as conn:
        rows = conn.execute(
            text("SELECT details FROM audit_logs WHERE event_type = 'match_reviewed'")
        ).all()
    assert len(rows) == 1
    import json as _json

    payload = _json.loads(rows[0][0])
    assert payload["match_id"] == str(match_id)
    assert "sparse JD" in payload["note"]


def test_note_endpoint_returns_404_for_unknown_match(client: TestClient) -> None:
    """Posting a note for a non-existent match must return 404."""
    response = client.post(f"/admin/scoring-review/{uuid.uuid4()}/note", json={"note": "x"})
    assert response.status_code == 404


def test_note_endpoint_rejects_empty_note(client: TestClient, session_factory) -> None:
    """An empty note must be rejected — the audit row would be useless."""
    match_id = _seed(session_factory, confidence=0.1)
    response = client.post(f"/admin/scoring-review/{match_id}/note", json={"note": ""})
    assert response.status_code == 422


def test_note_endpoint_rejects_overlong_note(client: TestClient, session_factory) -> None:
    """Notes over 2000 characters must be rejected to keep the audit log readable."""
    match_id = _seed(session_factory, confidence=0.1)
    response = client.post(
        f"/admin/scoring-review/{match_id}/note",
        json={"note": "x" * 2001},
    )
    assert response.status_code == 422
