"""Persistence gateway for the sources slice.

Three implementations live here:

* :class:`VacancyRepository` — Protocol the service layer depends on.
* :class:`InMemoryVacancyRepository` — dict-backed fake for tests.
* :class:`SqlVacancyRepository` — production implementation backed by a
  SQLAlchemy ``Session``.

The service layer is the only consumer of these classes; the API layer
goes through the service. Keeping the contract as a :class:`Protocol`
makes it easy to swap in a fake (or a future cached/async variant) without
touching the service.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from typing import Any, Protocol

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from job_apply.features.sources.models import Vacancy


class VacancyRepository(Protocol):
    """Minimal interface the :class:`SourceService` relies on.

    The ``upsert`` operation is the only writer: it must behave like an
    ``INSERT ... ON CONFLICT (source, source_id) DO UPDATE`` so a re-ingest
    of the same natural key mutates the existing row in place (preserving
    the canonical ``id`` and ``created_at``).
    """

    def upsert(self, vacancy: Vacancy) -> Vacancy: ...
    def get_by_id(self, vacancy_id: uuid.UUID) -> Vacancy | None: ...
    def list_by_source(self, source: str) -> Sequence[Vacancy]: ...
    def list_recent(self, *, limit: int) -> Sequence[Vacancy]: ...


# ---------------------------------------------------------------------------
# In-memory implementation
# ---------------------------------------------------------------------------


class InMemoryVacancyRepository:
    """Dict-backed repository for tests.

    Keys are the (source, source_id) pair; the same natural-key insert
    re-mutates the existing row in place, mirroring the SQL behaviour.
    """

    def __init__(self) -> None:
        self._by_id: dict[uuid.UUID, Vacancy] = {}
        self._by_source_id: dict[tuple[str, str], uuid.UUID] = {}

    def upsert(self, vacancy: Vacancy) -> Vacancy:
        key = (vacancy.source, vacancy.source_id)
        existing_id = self._by_source_id.get(key)

        now = datetime.now(UTC)
        if existing_id is not None:
            # Update: keep the original id and created_at; bump updated_at.
            existing = self._by_id[existing_id]
            vacancy.id = existing.id
            vacancy.created_at = existing.created_at
            vacancy.updated_at = now
            for attr in (
                "title",
                "description",
                "url",
                "salary_from",
                "salary_to",
                "salary_currency",
                "salary_gross",
                "employer_name",
                "location",
                "schedule",
                "experience",
                "skills",
                "published_at",
                "source_updated_at",
                "raw_data",
                "content_hash",
            ):
                setattr(existing, attr, getattr(vacancy, attr))
            return existing

        # Insert: assign id and timestamps.
        if vacancy.id is None:
            vacancy.id = uuid.uuid4()
        vacancy.created_at = now
        vacancy.updated_at = now
        self._by_id[vacancy.id] = vacancy
        self._by_source_id[key] = vacancy.id
        return vacancy

    def get_by_id(self, vacancy_id: uuid.UUID) -> Vacancy | None:
        return self._by_id.get(vacancy_id)

    def list_by_source(self, source: str) -> Sequence[Vacancy]:
        return [v for v in self._by_id.values() if v.source == source]

    def list_recent(self, *, limit: int) -> Sequence[Vacancy]:
        sentinel = datetime.min.replace(tzinfo=UTC)
        ordered = sorted(
            self._by_id.values(),
            key=lambda v: v.created_at or sentinel,
            reverse=True,
        )
        return ordered[:limit]


# ---------------------------------------------------------------------------
# SQLAlchemy implementation
# ---------------------------------------------------------------------------


# Defaults applied by the SQL upsert when a not-null column is left
# ``None`` by the caller. The ORM machinery applies these defaults when
# you ``session.add()`` an instance, but Core-level ``INSERT`` statements
# skip them — so we re-apply them here as a safety net.
_NOT_NULL_COLUMN_DEFAULTS: dict[str, Any] = {
    "salary_currency": "RUR",
    "salary_gross": False,
}


def _upsert_columns(vacancy: Vacancy) -> dict[str, Any]:
    """Return the column→value mapping for the upsert statement.

    Excludes the natural key columns and the audit timestamps, which
    SQLAlchemy/the database manage on their own.
    """
    return {
        "title": vacancy.title,
        "description": vacancy.description,
        "url": vacancy.url,
        "salary_from": vacancy.salary_from,
        "salary_to": vacancy.salary_to,
        "salary_currency": vacancy.salary_currency,
        "salary_gross": vacancy.salary_gross,
        "employer_name": vacancy.employer_name,
        "location": vacancy.location,
        "schedule": vacancy.schedule,
        "experience": vacancy.experience,
        "skills": vacancy.skills,
        "published_at": vacancy.published_at,
        "source_updated_at": vacancy.source_updated_at,
        "raw_data": vacancy.raw_data,
        "content_hash": vacancy.content_hash,
    }


class SqlVacancyRepository:
    """SQLAlchemy-backed repository.

    Construct with either a fixed ``Session`` (caller-managed lifetime)
    or a ``session_factory`` callable (the FastAPI ``get_db`` pattern).
    """

    def __init__(
        self,
        *,
        session_factory: Callable[[], Session] | None = None,
    ) -> None:
        self._session_factory = session_factory

    def _scope(self) -> Session:
        if self._session_factory is None:
            raise RuntimeError("SqlVacancyRepository is not bound to a session")
        return self._session_factory()

    def upsert(self, vacancy: Vacancy) -> Vacancy:
        """Insert or update the vacancy keyed on ``(source, source_id)``.

        Uses the dialect-native ``ON CONFLICT`` / ``ON DUPLICATE KEY``
        construct so we get a single round-trip even when the row exists.
        """
        session = self._scope()
        try:
            # Core-level ``INSERT`` statements do not run the ORM column
            # defaults, so fall back to the model defaults for any
            # not-null column the caller left unset.
            row_values: dict[str, Any] = {
                "id": vacancy.id if vacancy.id is not None else uuid.uuid4(),
                "source": vacancy.source,
                "source_id": vacancy.source_id,
            }
            for col, value in _upsert_columns(vacancy).items():
                if value is None and col in _NOT_NULL_COLUMN_DEFAULTS:
                    value = _NOT_NULL_COLUMN_DEFAULTS[col]
                row_values[col] = value

            dialect = session.bind.dialect.name if session.bind is not None else "sqlite"
            if dialect == "postgresql":
                insert_stmt = pg_insert(Vacancy).values(**row_values)
                update_cols = {
                    col: getattr(insert_stmt.excluded, col) for col in _upsert_columns(vacancy)
                }
                insert_stmt = insert_stmt.on_conflict_do_update(
                    constraint="uq_vacancies_source_source_id",
                    set_=update_cols,
                )
            else:
                # SQLite (and the generic dialect) — ``prefix_with("OR REPLACE")``
                # would clobber the row, so we use the ``INSERT ... ON CONFLICT``
                # form supported by sqlite ≥ 3.24.
                insert_stmt = sqlite_insert(Vacancy).values(**row_values)
                update_cols = {
                    col: getattr(insert_stmt.excluded, col) for col in _upsert_columns(vacancy)
                }
                insert_stmt = insert_stmt.on_conflict_do_update(
                    index_elements=["source", "source_id"],
                    set_=update_cols,
                )

            session.execute(insert_stmt)
            session.commit()

            # Re-fetch the canonical row so the caller observes the
            # database-assigned id and the (server-side) created_at.
            refreshed = session.execute(
                select(Vacancy).where(
                    Vacancy.source == vacancy.source,
                    Vacancy.source_id == vacancy.source_id,
                )
            ).scalar_one()
            return refreshed
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def get_by_id(self, vacancy_id: uuid.UUID) -> Vacancy | None:
        session = self._scope()
        try:
            return session.get(Vacancy, vacancy_id)
        finally:
            session.close()

    def list_by_source(self, source: str) -> Sequence[Vacancy]:
        session = self._scope()
        try:
            statement = (
                select(Vacancy).where(Vacancy.source == source).order_by(Vacancy.created_at.desc())
            )
            return list(session.execute(statement).scalars().all())
        finally:
            session.close()

    def list_recent(self, *, limit: int) -> Sequence[Vacancy]:
        session = self._scope()
        try:
            statement = select(Vacancy).order_by(Vacancy.created_at.desc()).limit(limit)
            return list(session.execute(statement).scalars().all())
        finally:
            session.close()


__all__ = [
    "InMemoryVacancyRepository",
    "SqlVacancyRepository",
    "VacancyRepository",
]
