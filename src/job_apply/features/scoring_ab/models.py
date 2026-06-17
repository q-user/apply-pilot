"""ORM models for the A/B scoring experiment slice (issue #65).

Three tables back the slice:

* ``scoring_experiments`` — one row per experiment. ``name`` is the
  experiment's stable identifier (e.g. ``"vacancy_scoring"``); the
  partial UNIQUE INDEX on ``(name) WHERE active`` enforces the "at
  most one active experiment per name" invariant at the DB level.
* ``scoring_experiment_variants`` — child collection of the experiment;
  one row per variant with its prompt version and weight. The
  ``(experiment_id, name)`` pair is unique so a single experiment
  cannot have two variants with the same name.
* ``scoring_experiment_outcomes`` — append-only log of scoring events.
  Each row records which variant the user landed in, the score the
  LLM produced, and whether the user eventually accepted the match.
  The score and the acceptance flag are stored as ``Float`` and
  ``Boolean`` respectively so the same DDL works on both sqlite and
  PostgreSQL.

The :class:`ScoringExperiment` and :class:`ScoringVariant` *value
objects* (and the repository / service / router code) live in
:mod:`job_apply.features.scoring_ab.experiments`; this file only owns
the SQL schema so :mod:`Base.metadata.create_all` and Alembic can pick
the tables up uniformly with the rest of the slices.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    String,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from job_apply.db import Base
from job_apply.shared.types import GUID


class ScoringExperimentRow(Base):
    """A persisted scoring A/B experiment.

    The ``name`` column is the experiment's stable identifier; the
    partial UNIQUE INDEX on ``(name) WHERE active`` ensures at most
    one experiment is active for a given name at a time.
    """

    __tablename__ = "scoring_experiments"
    __table_args__ = (
        Index(
            "ix_scoring_experiments_one_active_per_name",
            "name",
            unique=True,
            sqlite_where=text("active"),
            postgresql_where=text("active"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    prompt_name: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return (
            f"ScoringExperimentRow(id={self.id!s}, name={self.name!r}, "
            f"prompt_name={self.prompt_name!r}, active={self.active!s})"
        )


class ScoringVariantRow(Base):
    """A single variant of a :class:`ScoringExperimentRow`.

    The ``(experiment_id, name)`` pair is unique so two variants in the
    same experiment cannot share a name. ``weight`` is the variant's
    probability mass; the sum of weights across an experiment must
    equal ~1.0 (enforced in the repository layer).
    """

    __tablename__ = "scoring_experiment_variants"
    __table_args__ = (
        Index(
            "uq_scoring_experiment_variants_experiment_name",
            "experiment_id",
            "name",
            unique=True,
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=uuid.uuid4)
    experiment_id: Mapped[uuid.UUID] = mapped_column(
        GUID(),
        ForeignKey("scoring_experiments.id", ondelete="CASCADE"),
        nullable=False,
    )
    name: Mapped[str] = mapped_column(String(64), nullable=False)
    prompt_version: Mapped[str] = mapped_column(String(50), nullable=False)
    weight: Mapped[float] = mapped_column(Float, nullable=False)

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return (
            f"ScoringVariantRow(id={self.id!s}, experiment_id={self.experiment_id!s}, "
            f"name={self.name!r}, prompt_version={self.prompt_version!r}, weight={self.weight})"
        )


class ScoringExperimentOutcomeRow(Base):
    """An append-only record of one scoring event for one variant.

    The ``user_id`` and ``vacancy_id`` columns reference the
    corresponding slices logically — no ``ForeignKey`` constraints are
    enforced so that deleting users / vacancies does not cascade the
    audit history away.
    """

    __tablename__ = "scoring_experiment_outcomes"
    __table_args__ = (
        Index(
            "ix_scoring_experiment_outcomes_experiment_id",
            "experiment_id",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=uuid.uuid4)
    experiment_id: Mapped[uuid.UUID] = mapped_column(
        GUID(),
        ForeignKey("scoring_experiments.id", ondelete="CASCADE"),
        nullable=False,
    )
    variant_name: Mapped[str] = mapped_column(String(64), nullable=False)
    user_id: Mapped[uuid.UUID] = mapped_column(GUID(), nullable=False)
    vacancy_id: Mapped[uuid.UUID] = mapped_column(GUID(), nullable=False)
    score: Mapped[float] = mapped_column(Float, nullable=False)
    accepted: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return (
            f"ScoringExperimentOutcomeRow(id={self.id!s}, "
            f"experiment_id={self.experiment_id!s}, variant_name={self.variant_name!r}, "
            f"score={self.score}, accepted={self.accepted!s})"
        )


__all__ = [
    "ScoringExperimentOutcomeRow",
    "ScoringExperimentRow",
    "ScoringVariantRow",
]
