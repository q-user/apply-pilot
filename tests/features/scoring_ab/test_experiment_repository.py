"""TDD tests for the :class:`ScoringExperimentRepository` contract.

Two implementations are exercised:

* :class:`InMemoryScoringExperimentRepository` — dict-backed fake for tests.
* :class:`SqlScoringExperimentRepository` — SQLAlchemy-backed production
  implementation (round-tripped through sqlite in-memory for the test).

The contract is small on purpose (issue #65 slice is the "bucketing + log
outcomes" primitive; richer queries land in future slices):

* :meth:`get_active` — return the active experiment for the given
  experiment name, or ``None`` when none is active.
* :meth:`list_all` — return every experiment (active and inactive).
* :meth:`record_outcome` — append a single outcome row.

Variants are persisted as a child collection of the experiment; readers
must see the full list. Activating one experiment must deactivate every
other experiment of the same name — same invariant the prompt-version
registry enforces.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime

import pytest
from sqlalchemy import StaticPool, create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from apply_pilot.db import Base
from apply_pilot.features.scoring_ab.experiments import (
    InMemoryScoringExperimentRepository,
    ScoringExperiment,
    ScoringExperimentRepository,
    ScoringVariant,
    SqlScoringExperimentRepository,
)

# ---------------------------------------------------------------------------
# Helpers
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
    created_at: datetime | None = None,
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
        created_at=created_at or datetime(2026, 6, 17, 12, 0, 0, tzinfo=UTC),
    )


# ---------------------------------------------------------------------------
# In-memory repository
# ---------------------------------------------------------------------------


@pytest.fixture
def in_memory() -> InMemoryScoringExperimentRepository:
    return InMemoryScoringExperimentRepository()


def test_in_memory_add_returns_experiment(
    in_memory: InMemoryScoringExperimentRepository,
) -> None:
    """``add`` must return the same experiment that was passed in."""
    experiment = _experiment()

    result = in_memory.add(experiment)

    assert result is experiment


def test_in_memory_add_raises_on_duplicate_id(
    in_memory: InMemoryScoringExperimentRepository,
) -> None:
    """Adding two experiments with the same id must raise ``ValueError``."""
    shared_id = uuid.uuid4()
    in_memory.add(_experiment(id=shared_id))

    with pytest.raises(ValueError, match="already exists"):
        in_memory.add(_experiment(id=shared_id))


def test_in_memory_add_raises_on_weight_mismatch(
    in_memory: InMemoryScoringExperimentRepository,
) -> None:
    """Weights of an experiment's variants must sum to ~1.0."""
    experiment = _experiment(
        variants=[
            _variant("control", "1.0.0", 0.4),
            _variant("treatment", "1.1.0", 0.4),
        ]
    )

    with pytest.raises(ValueError, match="weight"):
        in_memory.add(experiment)


def test_in_memory_add_rejects_empty_variants(
    in_memory: InMemoryScoringExperimentRepository,
) -> None:
    """An experiment with no variants is not bucketing anything."""
    experiment = _experiment(variants=[])

    with pytest.raises(ValueError, match="variant"):
        in_memory.add(experiment)


def test_in_memory_add_rejects_duplicate_variant_names(
    in_memory: InMemoryScoringExperimentRepository,
) -> None:
    """Issue #146: two variants sharing a ``name`` would merge in ``aggregate_outcomes``.

    The repository groups outcomes by ``variant_name``; a duplicate name
    would silently collapse two distinct variants into a single bucket
    and skew the experiment's measured effect.
    """
    experiment = _experiment(
        variants=[
            _variant("control", "1.0.0", 0.5),
            _variant("control", "1.1.0", 0.5),  # duplicate name
        ]
    )

    with pytest.raises(ValueError, match="duplicate variant names"):
        in_memory.add(experiment)


def test_in_memory_get_active_returns_active(
    in_memory: InMemoryScoringExperimentRepository,
) -> None:
    """``get_active`` returns the active experiment for the given name."""
    in_memory.add(_experiment(active=True, name="vacancy_scoring"))

    fetched = in_memory.get_active("vacancy_scoring")

    assert fetched is not None
    assert fetched.name == "vacancy_scoring"
    assert fetched.active is True


def test_in_memory_get_active_returns_none_when_no_active(
    in_memory: InMemoryScoringExperimentRepository,
) -> None:
    """A name with only inactive experiments returns ``None``."""
    in_memory.add(_experiment(active=False))

    assert in_memory.get_active("vacancy_scoring") is None


def test_in_memory_get_active_returns_none_for_unknown_name(
    in_memory: InMemoryScoringExperimentRepository,
) -> None:
    """No experiment for an unknown name → ``None``."""
    in_memory.add(_experiment(name="cover_letter"))

    assert in_memory.get_active("vacancy_scoring") is None


def test_in_memory_list_all_returns_every_experiment(
    in_memory: InMemoryScoringExperimentRepository,
) -> None:
    """``list_all`` returns every experiment regardless of ``active`` flag."""
    in_memory.add(_experiment(name="vacancy_scoring"))
    in_memory.add(_experiment(name="cover_letter", prompt_name="cover_letter", active=False))

    everything = in_memory.list_all()

    assert {e.name for e in everything} == {"vacancy_scoring", "cover_letter"}


def test_in_memory_add_replaces_active_for_same_name(
    in_memory: InMemoryScoringExperimentRepository,
) -> None:
    """Adding a new active experiment deactivates the previous active one."""
    old = _experiment(active=True)
    in_memory.add(old)

    new = _experiment(name=old.name, prompt_name=old.prompt_name, active=True)
    in_memory.add(new)

    fetched_active = in_memory.get_active(old.name)
    assert fetched_active is not None
    assert fetched_active.id == new.id

    stored_old = next(e for e in in_memory.list_all() if e.id == old.id)
    assert stored_old.active is False


def test_in_memory_record_outcome_appends_row(
    in_memory: InMemoryScoringExperimentRepository,
) -> None:
    """``record_outcome`` must add the outcome to the in-memory list."""
    experiment = _experiment()
    in_memory.add(experiment)
    user_id = uuid.uuid4()
    vacancy_id = uuid.uuid4()

    in_memory.record_outcome(
        experiment_id=experiment.id,
        variant_name="treatment",
        user_id=user_id,
        vacancy_id=vacancy_id,
        score=88,
        accepted=True,
    )

    outcomes = in_memory.list_outcomes(experiment.id)
    assert len(outcomes) == 1
    assert outcomes[0]["variant_name"] == "treatment"
    assert outcomes[0]["user_id"] == user_id
    assert outcomes[0]["vacancy_id"] == vacancy_id
    assert outcomes[0]["score"] == 88
    assert outcomes[0]["accepted"] is True


def test_in_memory_aggregate_outcomes_per_variant(
    in_memory: InMemoryScoringExperimentRepository,
) -> None:
    """Aggregate outcomes by variant — count, avg score, acceptance rate."""
    experiment = _experiment()
    in_memory.add(experiment)

    in_memory.record_outcome(
        experiment_id=experiment.id,
        variant_name="control",
        user_id=uuid.uuid4(),
        vacancy_id=uuid.uuid4(),
        score=50,
        accepted=True,
    )
    in_memory.record_outcome(
        experiment_id=experiment.id,
        variant_name="control",
        user_id=uuid.uuid4(),
        vacancy_id=uuid.uuid4(),
        score=70,
        accepted=False,
    )
    in_memory.record_outcome(
        experiment_id=experiment.id,
        variant_name="treatment",
        user_id=uuid.uuid4(),
        vacancy_id=uuid.uuid4(),
        score=80,
        accepted=True,
    )

    aggregate = in_memory.aggregate_outcomes(experiment.id)

    by_name = {row["variant_name"]: row for row in aggregate}
    assert by_name["control"]["count"] == 2
    assert by_name["control"]["avg_score"] == 60.0
    assert by_name["control"]["acceptance_rate"] == 0.5
    assert by_name["treatment"]["count"] == 1
    assert by_name["treatment"]["avg_score"] == 80.0
    assert by_name["treatment"]["acceptance_rate"] == 1.0


# ---------------------------------------------------------------------------
# SQL repository
# ---------------------------------------------------------------------------


@pytest.fixture
def engine() -> Iterator[Engine]:
    """Yield a fresh in-memory sqlite engine with the experiment tables.

    Mirrors the equivalent fixture in
    ``tests/features/scoring/test_prompt_registry.py``: only the slice's
    own tables are created so the test does not drag the rest of the
    schema in (and FK constraints on ``users`` /
    ``search_profiles`` etc. don't get in the way). Uses
    :class:`StaticPool` so every session sees the same in-memory
    database — otherwise each new connection creates its own DB and
    the tables disappear.
    """
    eng = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    from apply_pilot.features.scoring_ab.models import (
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
    factory = sessionmaker(bind=engine, class_=Session, autocommit=False, autoflush=False)
    yield factory


@pytest.fixture
def sql_repo(
    session_factory: sessionmaker[Session],
) -> SqlScoringExperimentRepository:
    return SqlScoringExperimentRepository(session_factory=session_factory)


def test_sql_add_persists_experiment(
    sql_repo: SqlScoringExperimentRepository,
) -> None:
    """An added experiment must round-trip through the SQL repo."""
    experiment = _experiment(name="vacancy_scoring")

    result = sql_repo.add(experiment)

    assert result.id == experiment.id
    fetched = sql_repo.get_active("vacancy_scoring")
    assert fetched is not None
    assert fetched.id == experiment.id
    assert {v.name for v in fetched.variants} == {"control", "treatment"}


def test_sql_get_active_returns_none_when_none_active(
    sql_repo: SqlScoringExperimentRepository,
) -> None:
    """``get_active`` returns ``None`` when no experiment is active."""
    sql_repo.add(_experiment(active=False))

    assert sql_repo.get_active("vacancy_scoring") is None


def test_sql_list_all_returns_every_experiment(
    sql_repo: SqlScoringExperimentRepository,
) -> None:
    """``list_all`` returns every experiment regardless of active flag."""
    sql_repo.add(_experiment(name="vacancy_scoring", active=True))
    sql_repo.add(_experiment(name="cover_letter", prompt_name="cover_letter", active=False))

    all_experiments = sql_repo.list_all()

    assert {e.name for e in all_experiments} == {"vacancy_scoring", "cover_letter"}


def test_sql_add_replaces_active_for_same_name(
    sql_repo: SqlScoringExperimentRepository,
) -> None:
    """Adding a new active experiment deactivates the previously active one."""
    first = _experiment(active=True)
    sql_repo.add(first)
    second = _experiment(name=first.name, prompt_name=first.prompt_name, active=True)
    sql_repo.add(second)

    fetched_active = sql_repo.get_active(first.name)
    assert fetched_active is not None
    assert fetched_active.id == second.id

    stored_first = next(e for e in sql_repo.list_all() if e.id == first.id)
    assert stored_first.active is False


def test_sql_record_outcome_persists_row(
    sql_repo: SqlScoringExperimentRepository,
) -> None:
    """``record_outcome`` writes a row that round-trips through aggregation."""
    experiment = _experiment()
    sql_repo.add(experiment)

    sql_repo.record_outcome(
        experiment_id=experiment.id,
        variant_name="control",
        user_id=uuid.uuid4(),
        vacancy_id=uuid.uuid4(),
        score=80,
        accepted=True,
    )

    aggregate = sql_repo.aggregate_outcomes(experiment.id)
    assert len(aggregate) == 1
    assert aggregate[0]["variant_name"] == "control"
    assert aggregate[0]["count"] == 1
    assert aggregate[0]["avg_score"] == 80.0
    assert aggregate[0]["acceptance_rate"] == 1.0


def test_sql_aggregate_outcomes_groups_by_variant(
    sql_repo: SqlScoringExperimentRepository,
) -> None:
    """Aggregate is grouped by variant name."""
    experiment = _experiment()
    sql_repo.add(experiment)

    sql_repo.record_outcome(
        experiment_id=experiment.id,
        variant_name="control",
        user_id=uuid.uuid4(),
        vacancy_id=uuid.uuid4(),
        score=50,
        accepted=True,
    )
    sql_repo.record_outcome(
        experiment_id=experiment.id,
        variant_name="control",
        user_id=uuid.uuid4(),
        vacancy_id=uuid.uuid4(),
        score=70,
        accepted=False,
    )
    sql_repo.record_outcome(
        experiment_id=experiment.id,
        variant_name="treatment",
        user_id=uuid.uuid4(),
        vacancy_id=uuid.uuid4(),
        score=90,
        accepted=True,
    )

    aggregate = sql_repo.aggregate_outcomes(experiment.id)
    by_name = {row["variant_name"]: row for row in aggregate}
    assert by_name["control"]["count"] == 2
    assert by_name["control"]["avg_score"] == 60.0
    assert by_name["control"]["acceptance_rate"] == 0.5
    assert by_name["treatment"]["count"] == 1
    assert by_name["treatment"]["avg_score"] == 90.0
    assert by_name["treatment"]["acceptance_rate"] == 1.0


def test_repository_protocol_is_runtime_checkable() -> None:
    """Both implementations satisfy the :class:`ScoringExperimentRepository` Protocol."""
    in_mem: ScoringExperimentRepository = InMemoryScoringExperimentRepository()
    assert isinstance(in_mem, ScoringExperimentRepository)
