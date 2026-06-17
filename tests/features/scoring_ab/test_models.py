"""TDD tests for the :class:`ScoringExperiment` value object.

The :class:`ScoringExperiment` and :class:`ScoringVariant` are the two
frozen dataclasses that anchor the ``features/scoring_ab`` slice.
They are pure data containers — no behavior beyond carrying the public
fields — and the tests in this module are mostly "frozen + carries
fields" sanity checks, modelled on the equivalent tests for
:class:`PromptVersion` in the prompt-version registry slice.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest

from apply_pilot.features.scoring_ab.experiments import (
    ScoringExperiment,
    ScoringVariant,
)


def _variant(
    name: str = "treatment",
    prompt_version: str = "1.0.0",
    weight: float = 0.5,
) -> ScoringVariant:
    """Build a fully-populated :class:`ScoringVariant` for tests."""
    return ScoringVariant(
        name=name,
        prompt_version=prompt_version,
        weight=weight,
    )


def _experiment(
    *,
    id: uuid.UUID | None = None,
    name: str = "vacancy_scoring",
    prompt_name: str = "vacancy_scoring",
    variants: list[ScoringVariant] | None = None,
    active: bool = True,
    created_at: datetime | None = None,
) -> ScoringExperiment:
    """Build a fully-populated :class:`ScoringExperiment` for tests."""
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
# ScoringVariant
# ---------------------------------------------------------------------------


def test_scoring_variant_is_frozen() -> None:
    """A :class:`ScoringVariant` is immutable — ``frozen=True``."""
    variant = _variant()

    with pytest.raises((AttributeError, Exception)):  # FrozenInstanceError
        variant.weight = 0.9  # type: ignore[misc]


def test_scoring_variant_carries_all_fields() -> None:
    """All three public fields are accessible on the dataclass."""
    variant = _variant(name="treatment", prompt_version="2.0.0", weight=0.7)

    assert variant.name == "treatment"
    assert variant.prompt_version == "2.0.0"
    assert variant.weight == 0.7


# ---------------------------------------------------------------------------
# ScoringExperiment
# ---------------------------------------------------------------------------


def test_scoring_experiment_is_frozen() -> None:
    """A :class:`ScoringExperiment` is immutable — ``frozen=True``."""
    experiment = _experiment()

    with pytest.raises((AttributeError, Exception)):  # FrozenInstanceError
        experiment.active = False  # type: ignore[misc]


def test_scoring_experiment_carries_all_fields() -> None:
    """All public fields are accessible on the dataclass."""
    variants = [_variant("control", "1.0.0", 0.5), _variant("treatment", "1.1.0", 0.5)]
    created = datetime(2026, 6, 17, 12, 0, 0, tzinfo=UTC)
    experiment = _experiment(
        id=uuid.UUID("11111111-1111-1111-1111-111111111111"),
        name="vacancy_scoring",
        prompt_name="vacancy_scoring",
        variants=variants,
        active=True,
        created_at=created,
    )

    assert experiment.id == uuid.UUID("11111111-1111-1111-1111-111111111111")
    assert experiment.name == "vacancy_scoring"
    assert experiment.prompt_name == "vacancy_scoring"
    assert experiment.variants == variants
    assert experiment.active is True
    assert experiment.created_at == created
