"""TDD tests for :class:`ScoringExperimentService` (issue #65).

The service is the public orchestration surface for the A/B testing
slice. It owns:

* :meth:`assign_variant` — deterministic, weight-based hash bucketing
  that maps a ``(user_id, experiment_name)`` pair to one of the
  experiment's variants. The same pair must always return the same
  variant; a uniform distribution over the configured weights is the
  only requirement on the hash.
* :meth:`record_outcome` — delegates to the underlying
  :class:`ScoringExperimentRepository` after a scoring run.

The slice's "live" wiring is exercised in
``test_scoring_integration.py``: the service sits between the
``ScoringService`` and the registry / experiment store.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest

from apply_pilot.features.scoring_ab.experiments import (
    InMemoryScoringExperimentRepository,
    ScoringExperiment,
    ScoringVariant,
)
from apply_pilot.features.scoring_ab.service import ScoringExperimentService


def _variant(name: str, prompt_version: str, weight: float) -> ScoringVariant:
    return ScoringVariant(name=name, prompt_version=prompt_version, weight=weight)


def _experiment(
    *,
    name: str = "vacancy_scoring",
    prompt_name: str = "vacancy_scoring",
    variants: list[ScoringVariant] | None = None,
    active: bool = True,
) -> ScoringExperiment:
    return ScoringExperiment(
        id=uuid.uuid4(),
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
def repo() -> InMemoryScoringExperimentRepository:
    return InMemoryScoringExperimentRepository()


@pytest.fixture
def service(repo: InMemoryScoringExperimentRepository) -> ScoringExperimentService:
    return ScoringExperimentService(repo)


# ---------------------------------------------------------------------------
# assign_variant
# ---------------------------------------------------------------------------


def test_assign_variant_returns_one_of_experiment_variants(
    service: ScoringExperimentService, repo: InMemoryScoringExperimentRepository
) -> None:
    """A fresh assignment must be one of the experiment's variants."""
    repo.add(_experiment())
    user_id = uuid.uuid4()

    variant = service.assign_variant(
        user_id=user_id, vacancy_id=uuid.uuid4(), experiment_name="vacancy_scoring"
    )

    assert variant.name in {"control", "treatment"}


def test_assign_variant_is_deterministic(
    service: ScoringExperimentService, repo: InMemoryScoringExperimentRepository
) -> None:
    """The same ``(user_id, experiment_name)`` must always return the same variant."""
    repo.add(_experiment())
    user_id = uuid.uuid4()
    vacancy_id = uuid.uuid4()

    first = service.assign_variant(
        user_id=user_id, vacancy_id=vacancy_id, experiment_name="vacancy_scoring"
    )
    second = service.assign_variant(
        user_id=user_id, vacancy_id=vacancy_id, experiment_name="vacancy_scoring"
    )
    third = service.assign_variant(
        user_id=user_id,
        vacancy_id=uuid.uuid4(),  # different vacancy id must NOT affect the bucket
        experiment_name="vacancy_scoring",
    )

    assert first.name == second.name == third.name


def test_assign_variant_is_independent_of_vacancy_id(
    service: ScoringExperimentService, repo: InMemoryScoringExperimentRepository
) -> None:
    """The bucket is keyed on ``(user_id, experiment_name)``, not on ``vacancy_id``."""
    repo.add(_experiment())
    user_id = uuid.uuid4()

    a = service.assign_variant(
        user_id=user_id, vacancy_id=uuid.uuid4(), experiment_name="vacancy_scoring"
    )
    b = service.assign_variant(
        user_id=user_id, vacancy_id=uuid.uuid4(), experiment_name="vacancy_scoring"
    )

    assert a.name == b.name


def test_assign_variant_returns_none_when_no_active_experiment(
    service: ScoringExperimentService, repo: InMemoryScoringExperimentRepository
) -> None:
    """No active experiment → ``None``; the scoring service falls back to the baseline."""
    repo.add(_experiment(active=False))

    assert (
        service.assign_variant(
            user_id=uuid.uuid4(), vacancy_id=uuid.uuid4(), experiment_name="vacancy_scoring"
        )
        is None
    )


def test_assign_variant_returns_none_for_unknown_experiment(
    service: ScoringExperimentService,
) -> None:
    """An unknown experiment name has no active experiment → ``None``."""
    assert (
        service.assign_variant(
            user_id=uuid.uuid4(), vacancy_id=uuid.uuid4(), experiment_name="nonexistent"
        )
        is None
    )


def test_assign_variant_distribution_follows_weights(
    service: ScoringExperimentService, repo: InMemoryScoringExperimentRepository
) -> None:
    """Across many users the empirical distribution must match the configured weights.

    Uses an 80/20 split with N=2000 users; the tolerance is wide enough
    to absorb hash-bucket noise on a small sample without making the
    test flaky. (5 percentage points in either direction is the worst
    case we have observed; 10pp gives plenty of headroom.)
    """
    repo.add(
        _experiment(
            variants=[
                _variant("control", "1.0.0", 0.8),
                _variant("treatment", "1.1.0", 0.2),
            ]
        )
    )

    counts: dict[str, int] = {"control": 0, "treatment": 0}
    n_users = 2000
    for _ in range(n_users):
        user_id = uuid.uuid4()
        variant = service.assign_variant(
            user_id=user_id, vacancy_id=uuid.uuid4(), experiment_name="vacancy_scoring"
        )
        assert variant is not None
        counts[variant.name] += 1

    control_pct = counts["control"] / n_users
    treatment_pct = counts["treatment"] / n_users
    assert 0.70 <= control_pct <= 0.90
    assert 0.10 <= treatment_pct <= 0.30


def test_assign_variant_changes_per_user_within_distribution(
    service: ScoringExperimentService, repo: InMemoryScoringExperimentRepository
) -> None:
    """Across many users both variants should be hit (smoke test on the hash)."""
    repo.add(_experiment())
    seen: set[str] = set()
    for _ in range(50):
        variant = service.assign_variant(
            user_id=uuid.uuid4(), vacancy_id=uuid.uuid4(), experiment_name="vacancy_scoring"
        )
        assert variant is not None
        seen.add(variant.name)
    assert seen == {"control", "treatment"}


# ---------------------------------------------------------------------------
# record_outcome
# ---------------------------------------------------------------------------


def test_record_outcome_appends_to_repository(
    service: ScoringExperimentService, repo: InMemoryScoringExperimentRepository
) -> None:
    """``record_outcome`` must call through to the underlying repository."""
    experiment = _experiment()
    repo.add(experiment)
    user_id = uuid.uuid4()
    vacancy_id = uuid.uuid4()

    service.record_outcome(
        experiment_id=experiment.id,
        variant_name="treatment",
        user_id=user_id,
        vacancy_id=vacancy_id,
        score=88,
        accepted=True,
    )

    outcomes = repo.list_outcomes(experiment.id)
    assert len(outcomes) == 1
    assert outcomes[0]["variant_name"] == "treatment"


def test_record_outcome_is_noop_for_unknown_experiment(
    service: ScoringExperimentService,
) -> None:
    """Recording an outcome for an unknown experiment is silently dropped.

    The service treats the record call as a fire-and-forget log
    statement; an FK violation against a deleted experiment must not
    break the scoring flow. The repository's own constraint check
    surfaces in the integration tests, but the service is intentionally
    lenient: operators may delete an experiment with no adverse effect
    on the hot path.
    """
    # Should not raise.
    service.record_outcome(
        experiment_id=uuid.uuid4(),
        variant_name="control",
        user_id=uuid.uuid4(),
        vacancy_id=uuid.uuid4(),
        score=80,
        accepted=True,
    )
