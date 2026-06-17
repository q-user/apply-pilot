"""Persistence gateway for the quick-filter vertical slice.

This module is the home of everything that turns the in-memory
:class:`FilterDecision` value object into durable state. It exposes:

* :class:`FilterDecisionRow` — the SQLAlchemy model.
* :class:`FilterDecisionRepository` — the Protocol every repository
  implementation must satisfy.
* :class:`InMemoryFilterDecisionRepository` — dict-backed fake for
  tests.
* :class:`SqlFilterDecisionRepository` — production implementation
  backed by a SQLAlchemy ``Session``.

Design choices
--------------

* The ``reasons`` column is a ``Text`` storing a JSON-encoded list of
  strings. ``Text`` is portable across sqlite (where ``JSON`` is just
  text) and PostgreSQL; encoding the list manually keeps the
  application-level schema independent of dialect-specific JSON column
  semantics.
* The ``(search_profile_id, created_at)`` composite index accelerates
  the listing queries that power the "show me the recent decisions
  for this profile" UI.
* Foreign keys cascade: deleting a :class:`SearchProfile` or
  :class:`Vacancy` removes its decisions rather than leaving orphan
  rows.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from typing import Protocol, runtime_checkable

from sqlalchemy import (
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    func,
    select,
)
from sqlalchemy.orm import Mapped, Session, mapped_column

from apply_pilot.db import Base
from apply_pilot.shared.types import GUID

# ---------------------------------------------------------------------------
# SQLAlchemy model
# ---------------------------------------------------------------------------


class FilterDecisionRow(Base):
    """A persisted quick-filter decision for a ``(profile, vacancy)`` pair.

    Mirrors the in-memory :class:`FilterDecision` value object but
    lives in the relational store. Each row captures:

    * which profile and vacancy the decision was made for;
    * the verdict (``"accept"`` or ``"reject"``);
    * the rule names that contributed to the verdict (a JSON-encoded
      list stored in ``Text``);
    * the version of the rule engine that produced the verdict — for
      later audits when the rule set evolves.

    The ``(search_profile_id, created_at)`` composite index is
    consulted by :meth:`SqlFilterDecisionRepository.list_by_profile`
    so the listing query stays fast as the table grows.
    """

    __tablename__ = "filter_decisions"
    __table_args__ = (
        Index(
            "ix_filter_decisions_profile_created",
            "search_profile_id",
            "created_at",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=uuid.uuid4)

    search_profile_id: Mapped[uuid.UUID] = mapped_column(
        GUID(),
        ForeignKey("search_profiles.id", ondelete="CASCADE"),
        nullable=False,
    )
    vacancy_id: Mapped[uuid.UUID] = mapped_column(
        GUID(),
        ForeignKey("vacancies.id", ondelete="CASCADE"),
        nullable=False,
    )

    decision: Mapped[str] = mapped_column(String(20), nullable=False)
    # JSON-encoded list of strings. ``Text`` is portable across sqlite
    # (which has no native JSON column type) and PostgreSQL.
    reasons: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    rule_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return (
            f"FilterDecisionRow(id={self.id!s}, "
            f"search_profile_id={self.search_profile_id!s}, "
            f"vacancy_id={self.vacancy_id!s}, decision={self.decision!r})"
        )


__all_models__ = ["FilterDecisionRow"]


# ---------------------------------------------------------------------------
# Protocol
# ---------------------------------------------------------------------------


@runtime_checkable
class FilterDecisionRepository(Protocol):
    """Minimal interface :class:`QuickFilterService` depends on."""

    def create(self, decision: FilterDecisionRow) -> FilterDecisionRow: ...
    def get_by_id(self, decision_id: uuid.UUID) -> FilterDecisionRow | None: ...
    def list_by_profile(
        self,
        profile_id: uuid.UUID,
        *,
        decision: str | None = None,
        limit: int = 20,
    ) -> Sequence[FilterDecisionRow]: ...
    def list_by_vacancy(self, vacancy_id: uuid.UUID) -> Sequence[FilterDecisionRow]: ...
    def count_by_decision(self, profile_id: uuid.UUID) -> dict[str, int]: ...


# ---------------------------------------------------------------------------
# In-memory implementation
# ---------------------------------------------------------------------------


class InMemoryFilterDecisionRepository:
    """Dict-backed repository for tests.

    The repository stores rows in a single dict keyed by id. The
    :meth:`list_by_profile` ordering uses ``created_at`` (descending)
    so the behaviour matches the SQL implementation.
    """

    def __init__(self) -> None:
        self._by_id: dict[uuid.UUID, FilterDecisionRow] = {}

    def create(self, decision: FilterDecisionRow) -> FilterDecisionRow:
        if decision.id is None:
            decision.id = uuid.uuid4()
        if decision.reasons is None:
            decision.reasons = "[]"
        if decision.created_at is None:
            decision.created_at = datetime.now(UTC)
        self._by_id[decision.id] = decision
        return decision

    def get_by_id(self, decision_id: uuid.UUID) -> FilterDecisionRow | None:
        return self._by_id.get(decision_id)

    def list_by_profile(
        self,
        profile_id: uuid.UUID,
        *,
        decision: str | None = None,
        limit: int = 20,
    ) -> Sequence[FilterDecisionRow]:
        rows = [r for r in self._by_id.values() if r.search_profile_id == profile_id]
        if decision is not None:
            rows = [r for r in rows if r.decision == decision]
        rows.sort(key=lambda r: r.created_at, reverse=True)
        return rows[:limit]

    def list_by_vacancy(self, vacancy_id: uuid.UUID) -> Sequence[FilterDecisionRow]:
        return [r for r in self._by_id.values() if r.vacancy_id == vacancy_id]

    def count_by_decision(self, profile_id: uuid.UUID) -> dict[str, int]:
        counts: dict[str, int] = {}
        for r in self._by_id.values():
            if r.search_profile_id != profile_id:
                continue
            counts[r.decision] = counts.get(r.decision, 0) + 1
        return counts


# ---------------------------------------------------------------------------
# SQLAlchemy implementation
# ---------------------------------------------------------------------------


class SqlFilterDecisionRepository:
    """SQLAlchemy-backed repository.

    Construct with either a fixed ``Session`` (caller-managed lifetime)
    or a ``session_factory`` callable (the FastAPI ``get_db`` pattern).
    The repository opens a short-lived session per operation and closes
    it before returning.
    """

    def __init__(
        self,
        *,
        session_factory: Callable[[], Session] | None = None,
    ) -> None:
        self._session_factory = session_factory

    def _scope(self) -> Session:
        if self._session_factory is None:
            raise RuntimeError("SqlFilterDecisionRepository is not bound to a session")
        return self._session_factory()

    # -- writers ---------------------------------------------------------

    def create(self, decision: FilterDecisionRow) -> FilterDecisionRow:
        session = self._scope()
        try:
            session.add(decision)
            session.commit()
            session.refresh(decision)
            return decision
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    # -- readers ---------------------------------------------------------

    def get_by_id(self, decision_id: uuid.UUID) -> FilterDecisionRow | None:
        session = self._scope()
        try:
            return session.get(FilterDecisionRow, decision_id)
        finally:
            session.close()

    def list_by_profile(
        self,
        profile_id: uuid.UUID,
        *,
        decision: str | None = None,
        limit: int = 20,
    ) -> Sequence[FilterDecisionRow]:
        session = self._scope()
        try:
            statement = (
                select(FilterDecisionRow)
                .where(FilterDecisionRow.search_profile_id == profile_id)
                .order_by(FilterDecisionRow.created_at.desc())
                .limit(limit)
            )
            if decision is not None:
                statement = statement.where(FilterDecisionRow.decision == decision)
            return list(session.execute(statement).scalars().all())
        finally:
            session.close()

    def list_by_vacancy(self, vacancy_id: uuid.UUID) -> Sequence[FilterDecisionRow]:
        session = self._scope()
        try:
            statement = select(FilterDecisionRow).where(FilterDecisionRow.vacancy_id == vacancy_id)
            return list(session.execute(statement).scalars().all())
        finally:
            session.close()

    def count_by_decision(self, profile_id: uuid.UUID) -> dict[str, int]:
        """Return ``{decision: count}`` for the given profile.

        Implemented with a single ``GROUP BY`` query — the only
        aggregation in the repository.
        """
        from sqlalchemy import func as sa_func

        session = self._scope()
        try:
            statement = (
                select(
                    FilterDecisionRow.decision,
                    sa_func.count(FilterDecisionRow.id),
                )
                .where(FilterDecisionRow.search_profile_id == profile_id)
                .group_by(FilterDecisionRow.decision)
            )
            return {  # noqa: C416
                decision: count for decision, count in session.execute(statement).all()
            }
        finally:
            session.close()


__all__ = [
    "FilterDecisionRepository",
    "FilterDecisionRow",
    "InMemoryFilterDecisionRepository",
    "SqlFilterDecisionRepository",
]
