"""TDD tests for the A/B testing admin endpoints (issue #65).

The endpoints are mounted at:

* ``GET /admin/scoring/experiments`` — list every experiment + its
  variants.
* ``GET /admin/scoring/experiments/{name}/outcomes`` — aggregate
  outcomes (count, avg score, acceptance rate) per variant for the
  experiment with the given ``name``.

Both endpoints are read-only and accept an injectable repository
through FastAPI's dependency-injection machinery.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import StaticPool, create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from job_apply.db import Base
from job_apply.features.scoring_ab.api import get_experiment_repo
from job_apply.features.scoring_ab.api import router as scoring_ab_router
from job_apply.features.scoring_ab.experiments import (
    ScoringExperiment,
    ScoringVariant,
    SqlScoringExperimentRepository,
)

# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------


def _variant(name: str, prompt_version: str, weight: float) -> ScoringVariant:
    return ScoringVariant(name=name, prompt_version=prompt_version, weight=weight)


def _experiment(
    *,
    id: uuid.UUID | None = None,
    name: str = "vacancy_scoring",
    prompt_name: str = "vacancy_scoring",
    variants: list[ScoringVariant] | None = None,
    active: bool = True,
) -> ScoringExperiment:
    return ScoringExperiment(
        id=id or uuid.uuid4(),
        name=name,
        prompt_name=prompt_name,
        variants=variants
        if variants is not None
        else [
            _variant("control", "1.0.0", 0.5),
            _variant("treatment", "1.1.0", 0.5),
        ],
        active=active,
        created_at=datetime(2026, 6, 17, 12, 0, 0, tzinfo=UTC),
    )


@pytest.fixture
def engine() -> Iterator[Engine]:
    """Yield an in-memory sqlite engine with the experiment tables.

    Uses :class:`StaticPool` so every session sees the same connection
    (and therefore the same in-memory database). Without ``StaticPool``,
    a fresh in-memory DB is created for every connection and the
    tables created via ``create_all`` disappear.
    """
    eng = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    from job_apply.features.scoring_ab.models import (
        ScoringExperimentOutcomeRow,
        ScoringExperimentRow,
        ScoringVariantRow,
    )

    Base.metadata.create_all(
        bind=eng,
        tables=[
            ScoringExperimentRow.__table__,
            ScoringVariantRow.__table__,
            ScoringExperimentOutcomeRow.__table__,
        ],
    )
    try:
        yield eng
    finally:
        eng.dispose()


@pytest.fixture
def session_factory(engine: Engine) -> Iterator[sessionmaker[Session]]:
    yield sessionmaker(bind=engine, class_=Session, autocommit=False, autoflush=False)


@pytest.fixture
def repo(session_factory: sessionmaker[Session]) -> SqlScoringExperimentRepository:
    return SqlScoringExperimentRepository(session_factory=session_factory)


@pytest.fixture
def client(repo: SqlScoringExperimentRepository) -> Iterator[TestClient]:
    app = FastAPI()
    app.include_router(scoring_ab_router)

    def _override_repo() -> SqlScoringExperimentRepository:
        return repo

    app.dependency_overrides[get_experiment_repo] = _override_repo
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# GET /admin/scoring/experiments
# ---------------------------------------------------------------------------


def test_list_experiments_returns_empty_list(client: TestClient) -> None:
    """No experiments in the store → empty list."""
    response = client.get("/admin/scoring/experiments")

    assert response.status_code == 200
    assert response.json() == []


def test_list_experiments_returns_all_with_variants(client: TestClient, repo) -> None:
    """Every experiment is returned with its full variant list."""
    repo.add(_experiment(name="vacancy_scoring"))
    repo.add(
        _experiment(
            name="cover_letter",
            prompt_name="cover_letter",
            variants=[_variant("control", "2.0.0", 1.0)],
        )
    )

    response = client.get("/admin/scoring/experiments")

    assert response.status_code == 200
    payload = response.json()
    by_name = {item["name"]: item for item in payload}
    assert set(by_name) == {"vacancy_scoring", "cover_letter"}
    assert {v["name"] for v in by_name["vacancy_scoring"]["variants"]} == {
        "control",
        "treatment",
    }
    assert {v["prompt_version"] for v in by_name["vacancy_scoring"]["variants"]} == {
        "1.0.0",
        "1.1.0",
    }


def test_list_experiments_response_shape(client: TestClient, repo) -> None:
    """Each row carries the expected public fields."""
    repo.add(_experiment())

    response = client.get("/admin/scoring/experiments")

    assert response.status_code == 200
    [row] = response.json()
    assert set(row.keys()) >= {"id", "name", "prompt_name", "active", "created_at", "variants"}
    variant = row["variants"][0]
    assert set(variant.keys()) >= {"name", "prompt_version", "weight"}


# ---------------------------------------------------------------------------
# GET /admin/scoring/experiments/{name}/outcomes
# ---------------------------------------------------------------------------


def test_outcomes_for_unknown_experiment_returns_404(client: TestClient) -> None:
    """An experiment name that does not exist must surface as ``404``."""
    response = client.get("/admin/scoring/experiments/nonexistent/outcomes")

    assert response.status_code == 404


def test_outcomes_returns_empty_when_no_data_yet(client: TestClient, repo) -> None:
    """An experiment with no outcomes → 200 + empty list."""
    repo.add(_experiment())

    response = client.get("/admin/scoring/experiments/vacancy_scoring/outcomes")

    assert response.status_code == 200
    payload = response.json()
    assert payload["experiment"]["name"] == "vacancy_scoring"
    assert payload["outcomes"] == []


def test_outcomes_aggregates_per_variant(client: TestClient, repo) -> None:
    """Per-variant aggregate (count, avg score, acceptance rate)."""
    experiment = _experiment()
    repo.add(experiment)

    repo.record_outcome(
        experiment_id=experiment.id,
        variant_name="control",
        user_id=uuid.uuid4(),
        vacancy_id=uuid.uuid4(),
        score=50,
        accepted=True,
    )
    repo.record_outcome(
        experiment_id=experiment.id,
        variant_name="control",
        user_id=uuid.uuid4(),
        vacancy_id=uuid.uuid4(),
        score=70,
        accepted=False,
    )
    repo.record_outcome(
        experiment_id=experiment.id,
        variant_name="treatment",
        user_id=uuid.uuid4(),
        vacancy_id=uuid.uuid4(),
        score=90,
        accepted=True,
    )

    response = client.get("/admin/scoring/experiments/vacancy_scoring/outcomes")

    assert response.status_code == 200
    payload = response.json()
    by_name = {row["variant_name"]: row for row in payload["outcomes"]}
    assert by_name["control"]["count"] == 2
    assert by_name["control"]["avg_score"] == 60.0
    assert by_name["control"]["acceptance_rate"] == 0.5
    assert by_name["treatment"]["count"] == 1
    assert by_name["treatment"]["avg_score"] == 90.0
    assert by_name["treatment"]["acceptance_rate"] == 1.0
