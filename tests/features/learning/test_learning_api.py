"""HTTP integration tests for the ``GET /learning/signals`` endpoint (M8, issue #63).

The endpoint is intentionally unauthenticated: it is an operational /
internal surface (mirrors the digest ``POST /digest/send`` style).
The query parameters are ``user_id`` (UUID) and ``limit`` (int, default
100, capped at 500) — ``user_id`` is required so a typo returns 422
instead of leaking every signal.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from apply_pilot.features.learning import models as _learning_models  # noqa: F401
from apply_pilot.features.learning.api import (
    get_learning_signals_service,
)
from apply_pilot.features.learning.api import (
    router as learning_router,
)
from apply_pilot.features.learning.repository import InMemoryLearningSignalRepository
from apply_pilot.features.learning.service import (
    LearningSignal,
    LearningSignalsService,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


_FIXED_TS = datetime(2026, 6, 17, 12, 0, 0, tzinfo=UTC)


def _signal(
    *,
    user_id: uuid.UUID | None = None,
    rejection_reason: str | None = "salary too low",
    score: float | None = 42.0,
    prompt_version: str | None = "1.0.0",
) -> LearningSignal:
    return LearningSignal(
        id=uuid.uuid4(),
        user_id=user_id or uuid.uuid4(),
        match_id=uuid.uuid4(),
        vacancy_id=uuid.uuid4(),
        search_profile_id=uuid.uuid4(),
        rejection_reason=rejection_reason,
        prompt_version=prompt_version,
        score=score,
        signal_type="rejection",
        created_at=_FIXED_TS,
    )


# ---------------------------------------------------------------------------
# API fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def learning_repo() -> InMemoryLearningSignalRepository:
    return InMemoryLearningSignalRepository()


@pytest.fixture
def app(
    learning_repo: InMemoryLearningSignalRepository,
) -> Iterator[FastAPI]:
    application = FastAPI()
    application.include_router(learning_router)
    application.dependency_overrides[get_learning_signals_service] = lambda: LearningSignalsService(
        repo=learning_repo
    )
    try:
        yield application
    finally:
        application.dependency_overrides.clear()


@pytest.fixture
def client(app: FastAPI) -> Iterator[TestClient]:
    with TestClient(app) as c:
        yield c


# ---------------------------------------------------------------------------
# GET /learning/signals
# ---------------------------------------------------------------------------


def test_get_signals_without_user_id_returns_422(client: TestClient) -> None:
    """``user_id`` is required — missing it must return 422."""
    response = client.get("/learning/signals")
    assert response.status_code == 422


def test_get_signals_with_invalid_uuid_returns_422(client: TestClient) -> None:
    """A non-UUID ``user_id`` must return 422, not 500."""
    response = client.get("/learning/signals", params={"user_id": "not-a-uuid"})
    assert response.status_code == 422


def test_get_signals_returns_empty_list_for_unknown_user(client: TestClient) -> None:
    """A user with no signals must receive an empty list, not a 404."""
    response = client.get("/learning/signals", params={"user_id": str(uuid.uuid4())})
    assert response.status_code == 200
    assert response.json() == []


def test_get_signals_returns_signals_for_user(
    client: TestClient, learning_repo: InMemoryLearningSignalRepository
) -> None:
    """The endpoint must return DTOs for every signal owned by ``user_id``."""
    user_id = uuid.uuid4()
    other_user = uuid.uuid4()
    mine = _signal(user_id=user_id, rejection_reason="not a fit", score=12.0)
    other = _signal(user_id=other_user)
    learning_repo.record(mine)
    learning_repo.record(other)

    response = client.get("/learning/signals", params={"user_id": str(user_id)})

    assert response.status_code == 200
    body = response.json()
    assert len(body) == 1
    assert body[0]["id"] == str(mine.id)
    assert body[0]["user_id"] == str(user_id)
    assert body[0]["rejection_reason"] == "not a fit"
    assert body[0]["score"] == 12.0
    assert body[0]["prompt_version"] == "1.0.0"
    assert body[0]["signal_type"] == "rejection"


def test_get_signals_respects_limit(
    client: TestClient, learning_repo: InMemoryLearningSignalRepository
) -> None:
    """``limit`` must cap the number of returned DTOs."""
    user_id = uuid.uuid4()
    for _ in range(3):
        learning_repo.record(_signal(user_id=user_id))

    response = client.get("/learning/signals", params={"user_id": str(user_id), "limit": 2})

    assert response.status_code == 200
    assert len(response.json()) == 2


def test_get_signals_rejects_non_integer_limit(client: TestClient) -> None:
    """A non-integer ``limit`` must return 422, not 500."""
    response = client.get(
        "/learning/signals",
        params={"user_id": str(uuid.uuid4()), "limit": "lots"},
    )
    assert response.status_code == 422


def test_get_signals_rejects_zero_limit(client: TestClient) -> None:
    """A non-positive ``limit`` must be rejected by the FastAPI validator."""
    response = client.get(
        "/learning/signals",
        params={"user_id": str(uuid.uuid4()), "limit": 0},
    )
    assert response.status_code == 422


def test_get_signals_rejects_oversized_limit(client: TestClient) -> None:
    """``limit`` is capped at 500 — over the cap must return 422."""
    response = client.get(
        "/learning/signals",
        params={"user_id": str(uuid.uuid4()), "limit": 1000},
    )
    assert response.status_code == 422
