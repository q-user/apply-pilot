"""FastAPI router for the A/B scoring experiment admin endpoints (issue #65).

The router is mounted at ``/admin/scoring`` (sibling of the existing
``/admin/integrations`` and ``/admin/health`` endpoints) and exposes
two read-only endpoints:

* ``GET /admin/scoring/experiments`` — list every experiment + its
  variants.
* ``GET /admin/scoring/experiments/{name}/outcomes`` — aggregate
  outcomes (count, avg score, acceptance rate) per variant for the
  experiment with the given ``name``.

Both endpoints accept an injectable
:class:`ScoringExperimentRepository` through FastAPI's
dependency-injection machinery; production wires the SQLAlchemy-backed
implementation, tests inject the in-memory fake. The endpoints are
intentionally unauthenticated for now — same posture as the rest of
the M6 admin slice.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from apply_pilot.db import get_db
from apply_pilot.features.scoring_ab.experiments import (
    ScoringExperimentRepository,
    SqlScoringExperimentRepository,
)

# ---------------------------------------------------------------------------
# Schemas (dict-shaped for now; can be promoted to Pydantic if the
# surface stabilises and a third endpoint joins)
# ---------------------------------------------------------------------------


def _scoring_variant_read(variant) -> dict[str, Any]:
    """Render a :class:`ScoringVariant` as a JSON-serialisable dict."""
    return {
        "name": variant.name,
        "prompt_version": variant.prompt_version,
        "weight": float(variant.weight),
    }


def _scoring_experiment_read(experiment) -> dict[str, Any]:
    """Render a :class:`ScoringExperiment` as a JSON-serialisable dict."""
    return {
        "id": str(experiment.id),
        "name": experiment.name,
        "prompt_name": experiment.prompt_name,
        "active": bool(experiment.active),
        "created_at": experiment.created_at.isoformat()
        if experiment.created_at is not None
        else None,
        "variants": [_scoring_variant_read(v) for v in experiment.variants],
    }


def _variant_outcome_read(aggregate: dict[str, Any]) -> dict[str, Any]:
    """Render a single aggregate row as a JSON-serialisable dict."""
    return {
        "variant_name": aggregate["variant_name"],
        "count": int(aggregate["count"]),
        "avg_score": float(aggregate["avg_score"]),
        "acceptance_rate": float(aggregate["acceptance_rate"]),
    }


# ---------------------------------------------------------------------------
# Dependency factory
# ---------------------------------------------------------------------------


def get_experiment_repo(
    session: Session = Depends(get_db),  # noqa: B008
) -> ScoringExperimentRepository:
    """FastAPI dependency: build a :class:`ScoringExperimentRepository`.

    Uses the request-scoped session from ``get_db``. The repository is
    constructed with the session directly (caller-managed lifetime) so
    the session survives the request boundary — ``get_db`` closes it
    after the response is sent. Tests override this dependency to
    inject an in-memory fake.
    """
    return SqlScoringExperimentRepository(session=session)


# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------


router: APIRouter = APIRouter(prefix="/admin/scoring", tags=["scoring-ab"])


# ---------------------------------------------------------------------------
# Route handlers
# ---------------------------------------------------------------------------


@router.get(
    "/experiments",
    response_model=list[dict[str, Any]],
    responses={
        200: {"description": "Every experiment with its variants."},
    },
    summary="List all A/B scoring experiments",
)
def list_experiments(
    repo: ScoringExperimentRepository = Depends(get_experiment_repo),  # noqa: B008
) -> list[dict[str, Any]]:
    """Return every experiment + its variants as a JSON-serialisable list."""
    return [_scoring_experiment_read(experiment) for experiment in repo.list_all()]


@router.get(
    "/experiments/{name}/outcomes",
    response_model=dict[str, Any],
    responses={
        200: {"description": "Aggregated outcomes per variant for the experiment."},
        404: {"description": "No experiment with the given name."},
    },
    summary="List aggregated outcomes for an experiment",
)
def list_experiment_outcomes(
    name: str,
    repo: ScoringExperimentRepository = Depends(get_experiment_repo),  # noqa: B008
) -> dict[str, Any]:
    """Return ``{experiment, outcomes}`` for the experiment with ``name``.

    The ``outcomes`` list is the per-variant aggregate (count,
    average score, acceptance rate). The experiment's name is
    matched against the ``name`` field; the endpoint returns
    ``404`` when no experiment with that name is found, mirroring
    the conventions of the other admin read endpoints.
    """
    experiments = repo.list_all()
    match = next((e for e in experiments if e.name == name), None)
    if match is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "code": "experiment_not_found",
                "message": f"no scoring experiment with name {name!r}",
            },
        )
    return {
        "experiment": _scoring_experiment_read(match),
        "outcomes": [_variant_outcome_read(row) for row in repo.aggregate_outcomes(match.id)],
    }


__all__ = [
    "get_experiment_repo",
    "list_experiment_outcomes",
    "list_experiments",
    "router",
]
