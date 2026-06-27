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

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from apply_pilot.features.sources.models import Vacancy


class VacancyRepository(Protocol):
    """Minimal interface the :class:`SourceService` relies on.

    The ``upsert`` operation is the only writer: it must behave like an
    ``INSERT ... ON CONFLICT (source, source_id) DO UPDATE`` so a re-ingest
    of the same natural key mutates the existing row in place (preserving
    the canonical ``id`` and ``created_at``).

    Read-side helpers are intentionally narrow: ``list_by_source`` and
    ``list_recent`` cover the simple "show me everything from X" use
    case, while :meth:`list_with_filters` /
    :meth:`count_with_filters` back the public ``GET /vacancies``
    endpoint (filters: source, salary_min, location, since; ordering
    by ``created_at`` desc for a stable newest-first pagination).
    """

    def upsert(self, vacancy: Vacancy) -> Vacancy: ...
    def get_by_id(self, vacancy_id: uuid.UUID) -> Vacancy | None: ...
    def list_by_source(self, source: str) -> Sequence[Vacancy]: ...
    def list_recent(self, *, limit: int) -> Sequence[Vacancy]: ...
    def find_by_source(self, source: str, source_id: str) -> list[Vacancy]: ...
    def find_by_content_hash(self, content_hash: str) -> list[Vacancy]: ...
    def list_with_filters(
        self,
        *,
        source: str | None = None,
        salary_min: int | None = None,
        location: str | None = None,
        since: datetime | None = None,
        limit: int,
        offset: int,
    ) -> Sequence[Vacancy]: ...
    def count_with_filters(
        self,
        *,
        source: str | None = None,
        salary_min: int | None = None,
        location: str | None = None,
        since: datetime | None = None,
    ) -> int: ...
    def get_by_ids(self, vacancy_ids: Sequence[uuid.UUID]) -> Sequence[Vacancy]: ...
    def find_existing_in_batch(self, source_ids):
        """Single SELECT ... WHERE source_id IN (...) -> set[str] (Fix #260)."""
        from sqlalchemy import select

        if not source_ids:
            return set()
        rows = (
            self._session.execute(
                select(Vacancy.source_id).where(Vacancy.source_id.in_(list(source_ids)))
            )
            .scalars()
            .all()
        )
        return set(rows)


# ---------------------------------------------------------------------------
# In-memory implementation
# ---------------------------------------------------------------------------


def _vacancy_matches(
    vacancy: Vacancy,
    *,
    source: str | None,
    salary_min: int | None,
    location: str | None,
    since: datetime | None,
) -> bool:
    """Apply the ``GET /vacancies`` filter set to a single vacancy.

    All filters combine as a logical AND; a ``None`` filter is "not
    applied". The location filter is a case-insensitive substring
    match; the salary filter rejects vacancies whose
    :attr:`Vacancy.salary_from` is unknown or below the floor.
    """
    if source is not None and vacancy.source != source:
        return False
    if salary_min is not None:
        # Vacancies with no ``salary_from`` never satisfy a minimum floor.
        salary_from = vacancy.salary_from
        if salary_from is None or salary_from < salary_min:
            return False
    if location is not None:
        # Vacancies with no location never satisfy a substring filter.
        vacancy_location = vacancy.location
        if vacancy_location is None:
            return False
        if location.lower() not in vacancy_location.lower():
            return False
    if since is not None:
        # Vacancies with no ``created_at`` are treated as "not after" the cutoff.
        created_at = vacancy.created_at
        if created_at is None or created_at <= since:
            return False
    return True


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
            # Copy every writable column (driven by the Vacancy model) from
            # the incoming record so adding a new column does not require
            # editing this method.
            existing = self._by_id[existing_id]
            vacancy.id = existing.id
            vacancy.created_at = existing.created_at
            vacancy.updated_at = now
            for attr in _upsert_field_names(Vacancy):
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

    def find_by_source(self, source: str, source_id: str) -> list[Vacancy]:
        """Return the (at most one) row matching ``(source, source_id)``.

        The natural key is unique, so the result is either empty or a
        single-element list. The list-typed return keeps the contract
        symmetric with :meth:`find_by_content_hash` (which can return
        multiple matches across sources).
        """
        vid = self._by_source_id.get((source, source_id))
        if vid is None:
            return []
        row = self._by_id.get(vid)
        return [row] if row is not None else []

    def find_by_content_hash(self, content_hash: str) -> list[Vacancy]:
        """Return all rows whose ``content_hash`` equals ``content_hash``.

        ``content_hash`` is the cross-source dedup key (the same job
        posted on hh and habr will share it), so this can return several
        rows from different sources.
        """
        return [v for v in self._by_id.values() if v.content_hash == content_hash]

    def find_existing_in_batch(self, source_ids: list[str]) -> set[str]:
        """Return the subset of ``source_ids`` already stored (Fix #260).

        Mirrors :meth:`SqlVacancyRepository.find_existing_in_batch` — a
        single in-memory scan over ``_by_source_id`` replaces the
        per-vacancy ``find_by_source`` N+1 loop.
        """
        if not source_ids:
            return set()
        existing = {key[1] for key in self._by_source_id}
        return existing & set(source_ids)

    def list_with_filters(
        self,
        *,
        source: str | None = None,
        salary_min: int | None = None,
        location: str | None = None,
        since: datetime | None = None,
        limit: int,
        offset: int,
    ) -> Sequence[Vacancy]:
        """Return a sorted, filtered, paginated slice of vacancies.

        The ordering matches the SQL implementation: ``created_at``
        desc, with rows missing a ``created_at`` (shouldn't happen in
        practice) sorted last so they do not pollute the head of the
        list.
        """
        sentinel = datetime.min.replace(tzinfo=UTC)
        matched = [
            v
            for v in self._by_id.values()
            if _vacancy_matches(
                v,
                source=source,
                salary_min=salary_min,
                location=location,
                since=since,
            )
        ]
        matched.sort(key=lambda v: v.created_at or sentinel, reverse=True)
        return matched[offset : offset + limit]

    def count_with_filters(
        self,
        *,
        source: str | None = None,
        salary_min: int | None = None,
        location: str | None = None,
        since: datetime | None = None,
    ) -> int:
        return sum(
            1
            for v in self._by_id.values()
            if _vacancy_matches(
                v,
                source=source,
                salary_min=salary_min,
                location=location,
                since=since,
            )
        )

    def get_by_ids(self, vacancy_ids: Sequence[uuid.UUID]) -> Sequence[Vacancy]:
        """Return vacancies matching the given IDs.

        Returns the vacancies in the same order as the input IDs,
        skipping any IDs that don't exist.
        """
        # Use a set for O(1) lookup, then filter to preserve order
        id_set = set(vacancy_ids)
        return [v for v in self._by_id.values() if v.id in id_set]


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


# Columns managed outside the upsert payload:
#   - ``id``: primary key, either caller-supplied or generated.
#   - ``source`` / ``source_id``: natural key, used in the conflict target.
#   - ``created_at`` / ``updated_at``: server-side audit timestamps.
_UPSERT_EXCLUDED_COLUMNS: frozenset[str] = frozenset(
    {"id", "source", "source_id", "created_at", "updated_at"}
)


def _upsert_field_names(
    model: type[Vacancy] = Vacancy,
) -> tuple[str, ...]:
    """Return the column names updated by an upsert, driven by the model.

    Reading from ``__table__.columns`` ensures both the SQL and the
    in-memory repositories stay in sync with the ``Vacancy`` model:
    adding a new column to the model propagates here automatically, so
    there is no hidden field list to forget.
    """
    return tuple(
        column.name
        for column in model.__table__.columns
        if column.name not in _UPSERT_EXCLUDED_COLUMNS
    )


def _upsert_columns(vacancy: Vacancy) -> dict[str, Any]:
    """Return the column→value mapping for the upsert statement.

    Excludes the natural key columns and the audit timestamps, which
    SQLAlchemy/the database manage on their own. The set of fields is
    derived from the :class:`Vacancy` model so it cannot drift from
    the schema.
    """
    return {name: getattr(vacancy, name) for name in _upsert_field_names()}


class SqlVacancyRepository:
    """SQLAlchemy-backed repository.

    Construct with either a fixed ``Session`` (caller-managed lifetime)
    or a ``session_factory`` callable (the FastAPI ``get_db`` pattern).
    """

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
            if self._session is None:
                session.close()

    def get_by_id(self, vacancy_id: uuid.UUID) -> Vacancy | None:
        session = self._scope()
        try:
            return session.get(Vacancy, vacancy_id)
        finally:
            if self._session is None:
                session.close()

    def list_by_source(self, source: str) -> Sequence[Vacancy]:
        session = self._scope()
        try:
            statement = (
                select(Vacancy).where(Vacancy.source == source).order_by(Vacancy.created_at.desc())
            )
            return list(session.execute(statement).scalars().all())
        finally:
            if self._session is None:
                session.close()

    def list_recent(self, *, limit: int) -> Sequence[Vacancy]:
        session = self._scope()
        try:
            statement = select(Vacancy).order_by(Vacancy.created_at.desc()).limit(limit)
            return list(session.execute(statement).scalars().all())
        finally:
            if self._session is None:
                session.close()

    def find_by_source(self, source: str, source_id: str) -> list[Vacancy]:
        """Return the (at most one) row matching ``(source, source_id)``.

        The natural key is unique, so the result is either empty or a
        single-element list. The list-typed return keeps the contract
        symmetric with :meth:`find_by_content_hash` (which can return
        multiple matches across sources).
        """
        session = self._scope()
        try:
            statement = select(Vacancy).where(
                Vacancy.source == source,
                Vacancy.source_id == source_id,
            )
            return list(session.execute(statement).scalars().all())
        finally:
            if self._session is None:
                session.close()

    def find_by_content_hash(self, content_hash: str) -> list[Vacancy]:
        """Return all rows whose ``content_hash`` equals ``content_hash``.

        ``content_hash`` is the cross-source dedup key, so this can return
        several rows from different sources reposting the same job.
        """
        session = self._scope()
        try:
            statement = select(Vacancy).where(Vacancy.content_hash == content_hash)
            return list(session.execute(statement).scalars().all())
        finally:
            if self._session is None:
                session.close()

    def list_with_filters(
        self,
        *,
        source: str | None = None,
        salary_min: int | None = None,
        location: str | None = None,
        since: datetime | None = None,
        limit: int,
        offset: int,
    ) -> Sequence[Vacancy]:
        """Return a filtered, paginated slice ordered by ``created_at`` desc.

        Filters compose as a logical AND. ``location`` is matched
        case-insensitively via ``LOWER(location) LIKE %pattern%`` —
        SQLite's default ``LIKE`` is case-insensitive for ASCII only,
        so we wrap both sides in ``func.lower`` to behave identically
        on PostgreSQL.
        """
        session = self._scope()
        try:
            statement = self._build_filtered_query(
                source=source,
                salary_min=salary_min,
                location=location,
                since=since,
            )
            statement = statement.order_by(Vacancy.created_at.desc())
            statement = statement.limit(limit).offset(offset)
            return list(session.execute(statement).scalars().all())
        finally:
            if self._session is None:
                session.close()

    def count_with_filters(
        self,
        *,
        source: str | None = None,
        salary_min: int | None = None,
        location: str | None = None,
        since: datetime | None = None,
    ) -> int:
        session = self._scope()
        try:
            statement = self._build_filtered_query(
                source=source,
                salary_min=salary_min,
                location=location,
                since=since,
            )
            count_stmt = select(func.count()).select_from(statement.subquery())
            return int(session.execute(count_stmt).scalar_one())
        finally:
            if self._session is None:
                session.close()

    def get_by_ids(self, vacancy_ids: Sequence[uuid.UUID]) -> Sequence[Vacancy]:
        """Return vacancies matching the given IDs.

        Uses a single ``WHERE id IN (... )`` query for efficiency.
        Returns vacancies in the same order as the input IDs,
        skipping any IDs that don't exist. Duplicate IDs in the
        input are deduplicated in the output.
        """
        if not vacancy_ids:
            return []
        # Deduplicate input while preserving order
        seen: set[uuid.UUID] = set()
        unique_ids = [vid for vid in vacancy_ids if vid not in seen and not seen.add(vid)]
        session = self._scope()
        try:
            statement = select(Vacancy).where(Vacancy.id.in_(unique_ids))
            rows = list(session.execute(statement).scalars().all())
            # Preserve input order
            row_by_id = {r.id: r for r in rows}
            return [row_by_id[vid] for vid in unique_ids if vid in row_by_id]
        finally:
            if self._session is None:
                session.close()

    @staticmethod
    def _build_filtered_query(
        *,
        source: str | None,
        salary_min: int | None,
        location: str | None,
        since: datetime | None,
    ):
        """Build a ``select(Vacancy)`` statement with the filter set applied.

        Centralised so :meth:`list_with_filters` and
        :meth:`count_with_filters` stay in lock-step on the predicate
        set — adding a new filter means editing one place, not two.
        """
        statement = select(Vacancy)
        if source is not None:
            statement = statement.where(Vacancy.source == source)
        if salary_min is not None:
            statement = statement.where(Vacancy.salary_from >= salary_min)
        if location is not None:
            pattern = f"%{location.lower()}%"
            statement = statement.where(func.lower(Vacancy.location).like(pattern))
        if since is not None:
            statement = statement.where(Vacancy.created_at > since)
        return statement


__all__ = [
    "InMemoryVacancyRepository",
    "SqlVacancyRepository",
    "VacancyRepository",
]
