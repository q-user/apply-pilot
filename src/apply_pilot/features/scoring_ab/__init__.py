"""A/B scoring experiment vertical slice (M8, issue #65).

This slice owns the deterministic hash-bucketing of incoming scoring
requests to one of N prompt variants and the append-only log of
outcomes that the comparison view consumes.

The slice sits next to :mod:`apply_pilot.features.scoring` (the "no
experiment" baseline path) and is intentionally a *sub-namespace*:
``features/scoring_ab/...``. Keeping it separate prevents the
A/B-specific machinery (variants, outcomes, weight sums) from
contaminating the simpler baseline.

Public surface
--------------

* :class:`ScoringVariant` — frozen dataclass: a single variant of an
  experiment (name, prompt version, weight).
* :class:`ScoringExperiment` — frozen dataclass: the full experiment
  definition.
* :class:`ScoringExperimentRepository` — :class:`typing.Protocol`
  every implementation satisfies.
* :class:`InMemoryScoringExperimentRepository` — dict-backed fake for
  tests.
* :class:`SqlScoringExperimentRepository` — SQLAlchemy-backed
  production implementation.
* :class:`ScoringExperimentService` — use-case service that exposes
  :meth:`~ScoringExperimentService.assign_variant` and
  :meth:`~ScoringExperimentService.record_outcome`.
* :data:`router` — FastAPI router mounted at ``/admin/scoring`` by
  :mod:`apply_pilot.app`.
* :func:`get_experiment_repo` — FastAPI dependency factory for the
  repository.
"""

from apply_pilot.features.scoring_ab.api import get_experiment_repo, router
from apply_pilot.features.scoring_ab.experiments import (
    InMemoryScoringExperimentRepository,
    ScoringExperiment,
    ScoringExperimentRepository,
    ScoringVariant,
    SqlScoringExperimentRepository,
)
from apply_pilot.features.scoring_ab.service import ScoringExperimentService

__all__ = [
    "InMemoryScoringExperimentRepository",
    "ScoringExperiment",
    "ScoringExperimentRepository",
    "ScoringExperimentService",
    "ScoringVariant",
    "SqlScoringExperimentRepository",
    "get_experiment_repo",
    "router",
]
