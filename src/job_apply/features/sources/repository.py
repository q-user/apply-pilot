"""Persistence gateway for the sources slice.

Three implementations:

* :class:`VacancyRepository` — Protocol the service layer depends on.
* :class:`InMemoryVacancyRepository` — dict-backed fake for tests.
* :class:`SqlVacancyRepository` — production implementation backed by
  a SQLAlchemy ``Session``.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from typing import Protocol

from sqlalchemy import select
from sqlalchemy.orm import Session

from job_apply.features.sources.models import Vacancy


class VacancyRepository(Protocol):
    """Minimal interface the ``SourceService`` relies on."""

    def upsert(self, vacancy: Vacancy) -> Vacancy: ...
    def get_by_id(self, vacancy_id: uuid.UUID) -> Vacancy | None: ...
    def list_by_source(self, source: str) -> Sequence[Vacancy]: ...
    def list_recent(self, *, limit: int) -> Sequence[Vacancy]: ...
    def find_by_source(self, source: str, source_id: str) -> Vacancy | None: ...
    def find_by_content_hash(self, content_hash: str) -> list[Vacancy]: ...


# ---------------------------------------------------------------------------
# In-memory implementation
# ---------------------------------------------------------------------------


class InMemoryVacancyRepository:
    """Dict-backed repository for tests."""

    def __init__(self) -> None:
        self._by_id: dict[uuid.UUID, Vacancy] = {}
        self._by_source_id: dict[tuple[str, str], uuid.UUID] = {}

    def upsert(self, vacancy: Vacancy) -> Vacancy:
        if vacancy.id is None:
            vacancy.id = uuid.uuid4()

        key = (vacancy.source, vacancy.source_id)
        existing_id = self._by_source_id.get(key)

        now = datetime.now(UTC)
        vacancy.updated_at = now

        if existing_id is not None:
            # Update existing record
            vacancy.id = existing_id
            vacancy.created_at = self._by_id[existing_id].created_at
            self._by_id[existing_id] = vacancy
        else:
            # Insert new record
            vacancy.created_at = now
            self._by_id[vacancy.id] = vacancy
            self._by_source_id[key] = vacancy.id

        return vacancy

    def get_by_id(self, vacancy_id: uuid.UUID) -> Vacancy | None:
        return self._by_id.get(vacancy_id)

    def list_by_source(self, source: str) -> Sequence[Vacancy]:
        return [v for v in self._by_id.values() if v.source == source]

    def list_recent(self, *, limit: int) -> Sequence[Vacancy]:
        sorted_vacancies = sorted(
            self._by_id.values(),
            key=lambda v: v.created_at or datetime.min.replace(tzinfo=UTC),
            reverse=True,
        )
        return sorted_vacancies[:limit]

    def find_by_source(self, source: str, source_id: str) -> Vacancy | None:
        vacancy_id = self._by_source_id.get((source, source_id))
        if vacancy_id is None:
            return None
        return self._by_id.get(vacancy_id)

    def find_by_content_hash(self, content_hash: str) -> list[Vacancy]:
        return [v for v in self._by_id.values() if v.content_hash == content_hash]


# ---------------------------------------------------------------------------
# SQLAlchemy implementation
# ---------------------------------------------------------------------------


class SqlVacancyRepository:
    """SQLAlchemy-backed repository.

    Construct with either a fixed ``Session`` (caller-managed lifetime) or
    a ``session_factory`` callable (the FastAPI ``get_db`` pattern).
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
        session = self._scope()
        try:
            # Use database-agnostic upsert: try insert, then update on conflict.
            existing = session.execute(
                select(Vacancy).where(
                    Vacancy.source == vacancy.source,
                    Vacancy.source_id == vacancy.source_id,
                )
            ).scalar_one_or_none()

            if existing is not None:
                # Update existing
                existing.title = vacancy.title
                existing.description = vacancy.description
                existing.url = vacancy.url
                existing.salary_from = vacancy.salary_from
                existing.salary_to = vacancy.salary_to
                existing.salary_currency = vacancy.salary_currency
                existing.salary_gross = vacancy.salary_gross
                existing.employer_name = vacancy.employer_name
                existing.location = vacancy.location
                existing.schedule = vacancy.schedule
                existing.experience = vacancy.experience
                existing.skills = vacancy.skills
                existing.published_at = vacancy.published_at
                existing.source_updated_at = vacancy.source_updated_at
                existing.raw_data = vacancy.raw_data
                existing.content_hash = vacancy.content_hash
                session.commit()
                session.refresh(existing)
                return existing

            session.add(vacancy)
            session.commit()
            session.refresh(vacancy)
            return vacancy
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

    def find_by_source(self, source: str, source_id: str) -> Vacancy | None:
        session = self._scope()
        try:
            statement = select(Vacancy).where(
                Vacancy.source == source,
                Vacancy.source_id == source_id,
            )
            return session.execute(statement).scalar_one_or_none()
        finally:
            session.close()

    def find_by_content_hash(self, content_hash: str) -> list[Vacancy]:
        session = self._scope()
        try:
            statement = select(Vacancy).where(Vacancy.content_hash == content_hash)
            return list(session.execute(statement).scalars().all())
        finally:
            session.close()


__all__ = [
    "InMemoryVacancyRepository",
    "SqlVacancyRepository",
    "VacancyRepository",
]
