"""Persistence gateway for the search_profiles slice.

Three implementations live here:

* :class:`SearchProfileRepository` — Protocol defining the contract the
  service layer depends on.
* :class:`InMemorySearchProfileRepository` — dict-backed fake for tests.
* :class:`SqlSearchProfileRepository` — production implementation backed
  by a SQLAlchemy ``Session``.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from typing import Protocol

from sqlalchemy import select
from sqlalchemy.orm import Session

from job_apply.features.search_profiles.models import SearchProfile


class SearchProfileRepository(Protocol):
    """Minimal interface the ``SearchProfileService`` relies on."""

    def create(self, profile: SearchProfile) -> SearchProfile: ...
    def get_by_id(self, profile_id: uuid.UUID) -> SearchProfile | None: ...
    def list_by_user(self, user_id: uuid.UUID) -> Sequence[SearchProfile]: ...
    def list_active(self) -> Sequence[SearchProfile]: ...
    def update(self, profile: SearchProfile) -> SearchProfile: ...
    def delete(self, profile: SearchProfile) -> None: ...


# ---------------------------------------------------------------------------
# In-memory implementation
# ---------------------------------------------------------------------------


class InMemorySearchProfileRepository:
    """Dict-backed repository for tests."""

    def __init__(self) -> None:
        self._by_id: dict[uuid.UUID, SearchProfile] = {}
        self._by_user: dict[uuid.UUID, list[uuid.UUID]] = {}

    def create(self, profile: SearchProfile) -> SearchProfile:
        if profile.id is None:
            profile.id = uuid.uuid4()
        if profile.is_active is None:
            profile.is_active = True
        profile.created_at = datetime.now(UTC)
        self._by_id[profile.id] = profile
        self._by_user.setdefault(profile.user_id, []).append(profile.id)
        return profile

    def get_by_id(self, profile_id: uuid.UUID) -> SearchProfile | None:
        return self._by_id.get(profile_id)

    def list_by_user(self, user_id: uuid.UUID) -> Sequence[SearchProfile]:
        ids = self._by_user.get(user_id, ())
        return [self._by_id[pid] for pid in ids]

    def list_active(self) -> Sequence[SearchProfile]:
        """Return every profile flagged ``is_active=True``.

        Used by the matches slice to fan out an ingest batch across
        every active search profile; inactive profiles are skipped.
        """
        return [p for p in self._by_id.values() if p.is_active]

    def update(self, profile: SearchProfile) -> SearchProfile:
        profile.updated_at = datetime.now(UTC)
        self._by_id[profile.id] = profile
        return profile

    def delete(self, profile: SearchProfile) -> None:
        self._by_id.pop(profile.id, None)
        user_profiles = self._by_user.get(profile.user_id, [])
        if profile.id in user_profiles:
            user_profiles.remove(profile.id)


# ---------------------------------------------------------------------------
# SQLAlchemy implementation
# ---------------------------------------------------------------------------


class SqlSearchProfileRepository:
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
            raise RuntimeError("SqlSearchProfileRepository is not bound to a session")
        return self._session_factory()

    def create(self, profile: SearchProfile) -> SearchProfile:
        session = self._scope()
        try:
            session.add(profile)
            session.commit()
            session.refresh(profile)
            return profile
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def get_by_id(self, profile_id: uuid.UUID) -> SearchProfile | None:
        session = self._scope()
        try:
            return session.get(SearchProfile, profile_id)
        finally:
            session.close()

    def list_by_user(self, user_id: uuid.UUID) -> Sequence[SearchProfile]:
        session = self._scope()
        try:
            statement = (
                select(SearchProfile)
                .where(SearchProfile.user_id == user_id)
                .order_by(SearchProfile.created_at.desc())
            )
            return list(session.execute(statement).scalars().all())
        finally:
            session.close()

    def list_active(self) -> Sequence[SearchProfile]:
        session = self._scope()
        try:
            statement = (
                select(SearchProfile)
                .where(SearchProfile.is_active.is_(True))
                .order_by(SearchProfile.created_at.desc())
            )
            return list(session.execute(statement).scalars().all())
        finally:
            session.close()

    def update(self, profile: SearchProfile) -> SearchProfile:
        session = self._scope()
        try:
            merged = session.merge(profile)
            session.commit()
            session.refresh(merged)
            return merged
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def delete(self, profile: SearchProfile) -> None:
        session = self._scope()
        try:
            session.delete(profile)
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()


__all__ = [
    "InMemorySearchProfileRepository",
    "SearchProfileRepository",
    "SqlSearchProfileRepository",
]
