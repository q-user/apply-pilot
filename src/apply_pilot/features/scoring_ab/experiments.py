"""Domain types and repositories for the scoring A/B experiment slice.

The module exposes:

* :class:`ScoringVariant` — frozen dataclass: a single variant of an
  experiment (name, prompt version, weight).
* :class:`ScoringExperiment` — frozen dataclass: the full experiment
  (id, name, prompt name, list of variants, active flag, created_at).
* :class:`ScoringExperimentRepository` — :class:`typing.Protocol` every
  implementation satisfies.
* :class:`InMemoryScoringExperimentRepository` — dict-backed fake for
  tests.
* :class:`SqlScoringExperimentRepository` — SQLAlchemy-backed production
  implementation, with the partial UNIQUE index on
  ``(name) WHERE active`` enforcing the "one active experiment per
  name" invariant at the DB level.

The :class:`ScoringExperimentService` and the FastAPI router live in
their own modules (:mod:`apply_pilot.features.scoring_ab.service` and
:mod:`apply_pilot.features.scoring_ab.api`); the schema is split this
way so the repository and the service can evolve independently and
tests can target each in isolation.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol, runtime_checkable

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from apply_pilot.features.scoring_ab.models import (
    ScoringExperimentOutcomeRow,
    ScoringExperimentRow,
    ScoringVariantRow,
)

# ---------------------------------------------------------------------------
# Value objects
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ScoringVariant:
    """A single variant of a :class:`ScoringExperiment`.

    Public surface (kept stable for the scoring service and the admin
    endpoints):

    * ``name`` — the variant's stable identifier (``"control"``,
      ``"treatment"``, ...). Unique within an experiment.
    * ``prompt_version`` — the SemVer string the LLM scorer should
      stamp on the result when the user is bucketed into this variant.
    * ``weight`` — probability mass in ``[0.0, 1.0]``. The sum of
      weights across an experiment must equal 1.0 (validated in the
      repository).
    """

    name: str
    prompt_version: str
    weight: float


@dataclass(frozen=True)
class ScoringExperiment:
    """A persisted A/B experiment definition.

    The dataclass is the canonical value object the rest of the slice
    reads and writes; the SQLAlchemy row classes in
    :mod:`apply_pilot.features.scoring_ab.models` exist only as a
    persistence detail.

    Public surface:

    * ``id`` — stable identifier of the experiment.
    * ``name`` — the experiment's lookup key (e.g.
      ``"vacancy_scoring"``).
    * ``prompt_name`` — the prompt template family the experiment
      belongs to (e.g. ``"vacancy_scoring"``). An experiment always
      belongs to exactly one prompt family; multiple experiments can
      target the same family but only one is active at a time.
    * ``variants`` — the list of variants the user is bucketed into.
    * ``active`` — whether the experiment is currently the active one
      for its name.
    * ``created_at`` — server-side timestamp with timezone.
    """

    id: uuid.UUID
    name: str
    prompt_name: str
    variants: list[ScoringVariant] = field(default_factory=list)
    active: bool = False
    created_at: datetime | None = None


# ---------------------------------------------------------------------------
# Protocol
# ---------------------------------------------------------------------------


@runtime_checkable
class ScoringExperimentRepository(Protocol):
    """Minimal contract the scoring service and admin endpoints rely on.

    The contract is deliberately small (issue #65 slice is the
    "bucketing + log outcomes" primitive; richer queries land in future
    slices). ``list_outcomes`` and ``aggregate_outcomes`` are
    administrative reads and live alongside the writer so the test
    fakes can back the read endpoints with one object.
    """

    def add(self, experiment: ScoringExperiment) -> ScoringExperiment: ...
    def get_active(self, name: str) -> ScoringExperiment | None: ...
    def list_all(self) -> list[ScoringExperiment]: ...
    def record_outcome(
        self,
        *,
        experiment_id: uuid.UUID,
        variant_name: str,
        user_id: uuid.UUID,
        vacancy_id: uuid.UUID,
        score: float,
        accepted: bool,
    ) -> None: ...
    def list_outcomes(self, experiment_id: uuid.UUID) -> list[dict[str, Any]]: ...
    def aggregate_outcomes(self, experiment_id: uuid.UUID) -> list[dict[str, Any]]: ...


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


#: Tolerance for "weights sum to 1.0" checks. Float arithmetic on user
#: input is rarely exact, so the repository layer treats anything within
#: ``_WEIGHT_SUM_TOLERANCE`` of 1.0 as valid.
_WEIGHT_SUM_TOLERANCE: float = 1e-6


def _validate_experiment(experiment: ScoringExperiment) -> None:
    """Raise :class:`ValueError` if ``experiment`` cannot be persisted.

    Two invariants are checked:

    * the experiment has at least one variant (an experiment with no
      variants cannot bucket anyone);
    * the variants' weights sum to ~1.0 (probability mass must be
      conserved).
    """
    if not experiment.variants:
        raise ValueError(f"experiment {experiment.name!r} must have at least one variant")
    total = sum(v.weight for v in experiment.variants)
    if abs(total - 1.0) > _WEIGHT_SUM_TOLERANCE:
        raise ValueError(f"experiment {experiment.name!r} weights must sum to 1.0, got {total}")


# ---------------------------------------------------------------------------
# In-memory implementation
# ---------------------------------------------------------------------------


class InMemoryScoringExperimentRepository:
    """Dict-backed repository for tests.

    Three internal indices make the read paths cheap:

    * ``_by_id`` — ``id`` → :class:`ScoringExperiment`.
    * ``_active_by_name`` — ``name`` → :class:`ScoringExperiment` (only
      the currently-active experiment lives here).
    * ``_outcomes`` — ``experiment_id`` → ``list[dict]`` of outcome
      rows. Test introspection helpers (:meth:`list_outcomes`,
      :meth:`aggregate_outcomes`) read directly off this list.
    """

    __slots__ = ("_by_id", "_active_by_name", "_outcomes")

    def __init__(self) -> None:
        self._by_id: dict[uuid.UUID, ScoringExperiment] = {}
        self._active_by_name: dict[str, ScoringExperiment] = {}
        self._outcomes: dict[uuid.UUID, list[dict[str, Any]]] = {}

    # -- writers ---------------------------------------------------------

    def add(self, experiment: ScoringExperiment) -> ScoringExperiment:
        """Insert (or replace) ``experiment``.

        When ``experiment.active`` is ``True``, every other
        experiment of the same ``name`` is deactivated in the same
        step so the "one active per name" invariant holds.
        """
        _validate_experiment(experiment)
        if experiment.id in self._by_id:
            raise ValueError(f"experiment already exists: {experiment.id}")
        self._by_id[experiment.id] = experiment
        if experiment.active:
            # Deactivate every other experiment of the same name.
            for existing in list(self._by_id.values()):
                if existing.name == experiment.name and existing.id != experiment.id:
                    self._by_id[existing.id] = _with_active(existing, False)
            self._active_by_name[experiment.name] = experiment
        return experiment

    def record_outcome(
        self,
        *,
        experiment_id: uuid.UUID,
        variant_name: str,
        user_id: uuid.UUID,
        vacancy_id: uuid.UUID,
        score: float,
        accepted: bool,
    ) -> None:
        """Append a single outcome row to the in-memory log."""
        self._outcomes.setdefault(experiment_id, []).append(
            {
                "experiment_id": experiment_id,
                "variant_name": variant_name,
                "user_id": user_id,
                "vacancy_id": vacancy_id,
                "score": float(score),
                "accepted": bool(accepted),
            }
        )

    # -- readers ---------------------------------------------------------

    def get_active(self, name: str) -> ScoringExperiment | None:
        return self._active_by_name.get(name)

    def list_all(self) -> list[ScoringExperiment]:
        return list(self._by_id.values())

    def list_outcomes(self, experiment_id: uuid.UUID) -> list[dict[str, Any]]:
        return list(self._outcomes.get(experiment_id, ()))

    def aggregate_outcomes(self, experiment_id: uuid.UUID) -> list[dict[str, Any]]:
        """Return ``{variant_name, count, avg_score, acceptance_rate}`` per variant."""
        outcomes = self._outcomes.get(experiment_id, ())
        grouped: dict[str, list[dict[str, Any]]] = {}
        for row in outcomes:
            grouped.setdefault(row["variant_name"], []).append(row)

        result: list[dict[str, Any]] = []
        for variant_name, rows in grouped.items():
            count = len(rows)
            avg_score = sum(r["score"] for r in rows) / count
            acceptance_rate = sum(1 for r in rows if r["accepted"]) / count
            result.append(
                {
                    "variant_name": variant_name,
                    "count": count,
                    "avg_score": avg_score,
                    "acceptance_rate": acceptance_rate,
                }
            )
        result.sort(key=lambda r: r["variant_name"])
        return result


def _with_active(experiment: ScoringExperiment, active: bool) -> ScoringExperiment:
    """Return a copy of ``experiment`` with the ``active`` flag flipped.

    The dataclass is frozen, so callers cannot mutate ``active`` in
    place; this helper builds a modified copy.
    """
    return ScoringExperiment(
        id=experiment.id,
        name=experiment.name,
        prompt_name=experiment.prompt_name,
        variants=list(experiment.variants),
        active=active,
        created_at=experiment.created_at,
    )


# ---------------------------------------------------------------------------
# SQLAlchemy implementation
# ---------------------------------------------------------------------------


def _row_to_experiment(
    row: ScoringExperimentRow, variants: list[ScoringVariant]
) -> ScoringExperiment:
    """Translate a :class:`ScoringExperimentRow` + variants into the value object."""
    return ScoringExperiment(
        id=row.id,
        name=row.name,
        prompt_name=row.prompt_name,
        variants=variants,
        active=bool(row.active),
        created_at=row.created_at,
    )


def _load_variants(session: Session, experiment_id: uuid.UUID) -> list[ScoringVariant]:
    """Load every variant for ``experiment_id`` in stable order."""
    rows: Sequence[ScoringVariantRow] = list(
        session.execute(
            select(ScoringVariantRow)
            .where(ScoringVariantRow.experiment_id == experiment_id)
            .order_by(ScoringVariantRow.name)
        )
        .scalars()
        .all()
    )
    return [
        ScoringVariant(
            name=r.name,
            prompt_version=r.prompt_version,
            weight=float(r.weight),
        )
        for r in rows
    ]


class SqlScoringExperimentRepository:
    """SQLAlchemy-backed repository.

    The repository can be constructed two ways:

    * With a single ``Session`` (caller-managed lifetime). Useful for
      FastAPI's per-request ``get_db`` — the session is closed by the
      dependency, not by the repository.
    * With a ``session_factory`` (default). The repository opens a
      short-lived session per operation and closes it before returning.

    The partial UNIQUE index on ``(name) WHERE active`` is the
    schema-level guarantee that two active experiments of the same
    name can never coexist.
    """

    __slots__ = ("_session", "_session_factory")

    def __init__(
        self,
        session: Session | None = None,
        *,
        session_factory: Callable[[], Session] | None = None,
    ) -> None:
        if session is not None and session_factory is not None:
            raise ValueError("pass either session or session_factory, not both")
        self._session = session
        self._session_factory = session_factory

    def _scope(self) -> Session:
        if self._session is not None:
            return self._session
        if self._session_factory is None:
            raise RuntimeError("SqlScoringExperimentRepository is not bound to a session")
        return self._session_factory()

    # -- writers ---------------------------------------------------------

    def add(self, experiment: ScoringExperiment) -> ScoringExperiment:
        """Insert ``experiment`` + every variant in a single transaction.

        When ``experiment.active`` is ``True``, every other active
        experiment of the same ``name`` is deactivated first; the
        partial UNIQUE index would reject a direct insert otherwise.
        """
        _validate_experiment(experiment)
        session = self._scope()
        try:
            if experiment.active:
                session.execute(
                    update(ScoringExperimentRow)
                    .where(
                        ScoringExperimentRow.name == experiment.name,
                        ScoringExperimentRow.active.is_(True),
                    )
                    .values(active=False)
                )
            row = ScoringExperimentRow(
                id=experiment.id,
                name=experiment.name,
                prompt_name=experiment.prompt_name,
                active=bool(experiment.active),
                created_at=experiment.created_at,  # type: ignore[arg-type]
            )
            session.add(row)
            for variant in experiment.variants:
                session.add(
                    ScoringVariantRow(
                        id=uuid.uuid4(),
                        experiment_id=experiment.id,
                        name=variant.name,
                        prompt_version=variant.prompt_version,
                        weight=variant.weight,
                    )
                )
            session.commit()
            session.refresh(row)
            return _row_to_experiment(row, list(experiment.variants))
        except Exception:
            session.rollback()
            raise
        finally:
            if self._session is None:
                session.close()

    def record_outcome(
        self,
        *,
        experiment_id: uuid.UUID,
        variant_name: str,
        user_id: uuid.UUID,
        vacancy_id: uuid.UUID,
        score: float,
        accepted: bool,
    ) -> None:
        """Append a single outcome row."""
        session = self._scope()
        try:
            session.add(
                ScoringExperimentOutcomeRow(
                    id=uuid.uuid4(),
                    experiment_id=experiment_id,
                    variant_name=variant_name,
                    user_id=user_id,
                    vacancy_id=vacancy_id,
                    score=float(score),
                    accepted=bool(accepted),
                )
            )
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            if self._session is None:
                session.close()

    # -- readers ---------------------------------------------------------

    def get_active(self, name: str) -> ScoringExperiment | None:
        session = self._scope()
        try:
            row = session.execute(
                select(ScoringExperimentRow).where(
                    ScoringExperimentRow.name == name,
                    ScoringExperimentRow.active.is_(True),
                )
            ).scalar_one_or_none()
            if row is None:
                return None
            variants = _load_variants(session, row.id)
            return _row_to_experiment(row, variants)
        finally:
            if self._session is None:
                session.close()

    def list_all(self) -> list[ScoringExperiment]:
        session = self._scope()
        try:
            rows: Sequence[ScoringExperimentRow] = list(
                session.execute(
                    select(ScoringExperimentRow).order_by(
                        ScoringExperimentRow.name, ScoringExperimentRow.created_at
                    )
                )
                .scalars()
                .all()
            )
            experiments: list[ScoringExperiment] = []
            for row in rows:
                variants = _load_variants(session, row.id)
                experiments.append(_row_to_experiment(row, variants))
            return experiments
        finally:
            if self._session is None:
                session.close()

    def list_outcomes(self, experiment_id: uuid.UUID) -> list[dict[str, Any]]:
        session = self._scope()
        try:
            rows: Sequence[ScoringExperimentOutcomeRow] = list(
                session.execute(
                    select(ScoringExperimentOutcomeRow)
                    .where(ScoringExperimentOutcomeRow.experiment_id == experiment_id)
                    .order_by(ScoringExperimentOutcomeRow.created_at)
                )
                .scalars()
                .all()
            )
            return [
                {
                    "experiment_id": r.experiment_id,
                    "variant_name": r.variant_name,
                    "user_id": r.user_id,
                    "vacancy_id": r.vacancy_id,
                    "score": float(r.score),
                    "accepted": bool(r.accepted),
                }
                for r in rows
            ]
        finally:
            if self._session is None:
                session.close()

    def aggregate_outcomes(self, experiment_id: uuid.UUID) -> list[dict[str, Any]]:
        """Group raw outcomes in SQL by ``variant_name``.

        Returns a list of ``{variant_name, count, avg_score, acceptance_rate}``
        rows ordered by ``variant_name``. The aggregation is done in
        Python here for portability across sqlite and PostgreSQL — the
        slice is too small to warrant a dialect-specific SQL view.
        """
        raw = self.list_outcomes(experiment_id)
        grouped: dict[str, list[dict[str, Any]]] = {}
        for row in raw:
            grouped.setdefault(row["variant_name"], []).append(row)

        result: list[dict[str, Any]] = []
        for variant_name, rows in grouped.items():
            count = len(rows)
            avg_score = sum(r["score"] for r in rows) / count
            acceptance_rate = sum(1 for r in rows if r["accepted"]) / count
            result.append(
                {
                    "variant_name": variant_name,
                    "count": count,
                    "avg_score": avg_score,
                    "acceptance_rate": acceptance_rate,
                }
            )
        result.sort(key=lambda r: r["variant_name"])
        return result


__all__ = [
    "InMemoryScoringExperimentRepository",
    "ScoringExperiment",
    "ScoringExperimentRepository",
    "ScoringVariant",
    "SqlScoringExperimentRepository",
]
