"""Persistence gateway for the matches slice.

Three implementations live here, mirroring the conventions used by the
``search_profiles`` and ``sources`` slices:

* :class:`VacancyMatchRepository` — Protocol the service layer depends
  on.
* :class:`InMemoryVacancyMatchRepository` — dict-backed fake for tests.
* :class:`SqlVacancyMatchRepository` — production implementation backed
  by a SQLAlchemy ``Session``.

The in-memory implementation optionally accepts a
``SearchProfileRepository`` so ``list_by_user`` can resolve which
profile ids belong to the requesting user. The SQL implementation
performs the equivalent filter as a JOIN in a single query.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from typing import Protocol

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from apply_pilot.features.matches.models import MatchStatus, VacancyMatch
from apply_pilot.features.search_profiles.models import SearchProfile
from apply_pilot.shared.errors import NotFoundError

# ---------------------------------------------------------------------------
# Protocol
# ---------------------------------------------------------------------------


class VacancyMatchRepository(Protocol):
    """Minimal interface the :class:`MatchService` relies on."""

    def create(self, match: VacancyMatch) -> VacancyMatch: ...
    def get_by_id(self, match_id: uuid.UUID) -> VacancyMatch | None: ...
    def list_by_profile(
        self,
        profile_id: uuid.UUID,
        *,
        status: str | None = None,
        limit: int = 20,
    ) -> Sequence[VacancyMatch]: ...
    def list_by_user(
        self,
        user_id: uuid.UUID,
        *,
        status: str | None = None,
    ) -> Sequence[VacancyMatch]: ...
    def find_existing(
        self, profile_id: uuid.UUID, vacancy_id: uuid.UUID
    ) -> VacancyMatch | None: ...
    def update_status(
        self,
        match_id: uuid.UUID,
        status: str,
        *,
        score: int | None = None,
    ) -> VacancyMatch: ...
    def update_scoring(
        self,
        match_id: uuid.UUID,
        *,
        score: int,
        explanation: str,
        prompt_version: str,
        confidence: float,
        scored_at: datetime,
    ) -> VacancyMatch: ...
    def list_pending(self, *, limit: int = 50) -> Sequence[VacancyMatch]: ...


# ---------------------------------------------------------------------------
# In-memory implementation
# ---------------------------------------------------------------------------


ProfileLister = Callable[[uuid.UUID], Sequence[SearchProfile]]


class InMemoryVacancyMatchRepository:
    """Dict-backed repository for tests.

    The repository tracks two indices:

    * ``_by_id`` — primary key lookup.
    * ``_by_pair`` — ``(search_profile_id, vacancy_id)`` uniqueness
      index, used by :meth:`find_existing` to detect duplicate inserts.

    ``list_by_user`` delegates to a caller-supplied callable that
    resolves the user's profile ids. The callable is optional; when
    omitted the method returns an empty list so the repository stays
    usable in tests that only exercise the per-profile surface.
    """

    def __init__(
        self,
        *,
        list_user_profiles: ProfileLister | None = None,
    ) -> None:
        self._by_id: dict[uuid.UUID, VacancyMatch] = {}
        self._by_pair: dict[tuple[uuid.UUID, uuid.UUID], uuid.UUID] = {}
        self._list_user_profiles = list_user_profiles

    def create(self, match: VacancyMatch) -> VacancyMatch:
        if match.id is None:
            match.id = uuid.uuid4()
        if not match.status:
            match.status = "new"
        now = datetime.now(UTC)
        match.created_at = now
        match.updated_at = now
        self._by_id[match.id] = match
        self._by_pair[(match.search_profile_id, match.vacancy_id)] = match.id
        return match

    def get_by_id(self, match_id: uuid.UUID) -> VacancyMatch | None:
        return self._by_id.get(match_id)

    def list_by_profile(
        self,
        profile_id: uuid.UUID,
        *,
        status: str | None = None,
        limit: int = 20,
    ) -> Sequence[VacancyMatch]:
        matches = [m for m in self._by_id.values() if m.search_profile_id == profile_id]
        if status is not None:
            matches = [m for m in matches if m.status == status]
        matches.sort(key=lambda m: m.created_at, reverse=True)
        return matches[:limit]

    def list_by_user(
        self,
        user_id: uuid.UUID,
        *,
        status: str | None = None,
    ) -> Sequence[VacancyMatch]:
        if self._list_user_profiles is None:
            return []
        profile_ids = {p.id for p in self._list_user_profiles(user_id)}
        matches = [m for m in self._by_id.values() if m.search_profile_id in profile_ids]
        if status is not None:
            matches = [m for m in matches if m.status == status]
        matches.sort(key=lambda m: m.created_at, reverse=True)
        return matches

    def find_existing(self, profile_id: uuid.UUID, vacancy_id: uuid.UUID) -> VacancyMatch | None:
        match_id = self._by_pair.get((profile_id, vacancy_id))
        return self._by_id.get(match_id) if match_id is not None else None

    def update_status(
        self,
        match_id: uuid.UUID,
        status: str,
        *,
        score: int | None = None,
    ) -> VacancyMatch:
        match = self._by_id.get(match_id)
        if match is None:
            raise NotFoundError(f"vacancy match {match_id} not found")
        match.status = status
        if score is not None:
            match.score = score
        match.updated_at = datetime.now(UTC)
        return match

    def update_scoring(
        self,
        match_id: uuid.UUID,
        *,
        score: int,
        explanation: str,
        prompt_version: str,
        confidence: float,
        scored_at: datetime,
    ) -> VacancyMatch:
        """Persist the LLM scoring outcome on a match.

        Also moves the row to :attr:`MatchStatus.SCORED` so the
        "scored" queue can be filtered without re-querying on score
        alone. Raises :class:`NotFoundError` if the match does not
        exist.
        """
        match = self._by_id.get(match_id)
        if match is None:
            raise NotFoundError(f"vacancy match {match_id} not found")
        match.score = score
        match.explanation = explanation
        match.prompt_version = prompt_version
        match.confidence = confidence
        match.scored_at = scored_at
        match.status = MatchStatus.SCORED.value
        match.updated_at = datetime.now(UTC)
        return match

    def list_pending(self, *, limit: int = 50) -> Sequence[VacancyMatch]:
        """Return matches in ``new``/``review`` with no score yet.

        The slice's :class:`ScoringService` drains this list to keep
        the queue shallow. Ordering is ``created_at`` ascending so the
        oldest match is scored first.
        """
        pending_statuses = {MatchStatus.NEW.value, MatchStatus.REVIEW.value}
        matches = [
            m for m in self._by_id.values() if m.status in pending_statuses and m.score is None
        ]
        matches.sort(key=lambda m: m.created_at)
        return matches[:limit]


# ---------------------------------------------------------------------------
# SQLAlchemy implementation
# ---------------------------------------------------------------------------


_UPSERT_COLUMNS = (
    "search_profile_id",
    "vacancy_id",
    "status",
    "score",
    "match_reason",
)


class SqlVacancyMatchRepository:
    """SQLAlchemy-backed repository.

    The repository opens a short-lived session per operation and closes
    it before returning. ``list_by_user`` performs a JOIN against
    ``search_profiles`` to filter by owner.
    """

    def __init__(
        self,
        *,
        session_factory: Callable[[], Session] | None = None,
    ) -> None:
        self._session_factory = session_factory

    def _scope(self) -> Session:
        if self._session_factory is None:
            raise RuntimeError("SqlVacancyMatchRepository is not bound to a session")
        return self._session_factory()

    # -- writers ---------------------------------------------------------

    def create(self, match: VacancyMatch) -> VacancyMatch:
        session = self._scope()
        try:
            session.add(match)
            session.commit()
            session.refresh(match)
            return match
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def update_status(
        self,
        match_id: uuid.UUID,
        status: str,
        *,
        score: int | None = None,
    ) -> VacancyMatch:
        session = self._scope()
        try:
            match = session.get(VacancyMatch, match_id)
            if match is None:
                raise NotFoundError(f"vacancy match {match_id} not found")
            match.status = status
            if score is not None:
                match.score = score
            session.commit()
            session.refresh(match)
            return match
        except NotFoundError:
            session.rollback()
            raise
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def update_scoring(
        self,
        match_id: uuid.UUID,
        *,
        score: int,
        explanation: str,
        prompt_version: str,
        confidence: float,
        scored_at: datetime,
    ) -> VacancyMatch:
        """Persist the LLM scoring outcome and move the row to ``scored``."""
        session = self._scope()
        try:
            match = session.get(VacancyMatch, match_id)
            if match is None:
                raise NotFoundError(f"vacancy match {match_id} not found")
            match.score = score
            match.explanation = explanation
            match.prompt_version = prompt_version
            match.confidence = confidence
            match.scored_at = scored_at
            match.status = MatchStatus.SCORED.value
            session.commit()
            session.refresh(match)
            return match
        except NotFoundError:
            session.rollback()
            raise
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def list_pending(self, *, limit: int = 50) -> Sequence[VacancyMatch]:
        """Return ``new``/``review`` matches that have not been scored yet."""
        session = self._scope()
        try:
            statement = (
                select(VacancyMatch)
                .where(
                    VacancyMatch.status.in_([MatchStatus.NEW.value, MatchStatus.REVIEW.value]),
                    VacancyMatch.score.is_(None),
                )
                .order_by(VacancyMatch.created_at.asc())
                .limit(limit)
            )
            return list(session.execute(statement).scalars().all())
        finally:
            session.close()

    def bulk_create_ignore_conflicts(
        self, matches: Sequence[VacancyMatch]
    ) -> Sequence[VacancyMatch]:
        """Insert the matches, ignoring rows that collide on the unique pair.

        Uses the dialect-native ``ON CONFLICT DO NOTHING`` (PostgreSQL) /
        ``INSERT OR IGNORE`` (SQLite) to atomically skip duplicates
        rather than raise.
        """
        if not matches:
            return []
        session = self._scope()
        try:
            rows = [
                {
                    "id": m.id if m.id is not None else uuid.uuid4(),
                    "search_profile_id": m.search_profile_id,
                    "vacancy_id": m.vacancy_id,
                    "status": m.status or "new",
                    "score": m.score,
                    "match_reason": m.match_reason,
                    "explanation": m.explanation,
                    "prompt_version": m.prompt_version,
                    "scored_at": m.scored_at,
                }
                for m in matches
            ]
            dialect = session.bind.dialect.name if session.bind is not None else "sqlite"
            if dialect == "postgresql":
                insert_stmt = pg_insert(VacancyMatch).values(rows)
                insert_stmt = insert_stmt.on_conflict_do_nothing(
                    constraint="uq_vacancy_matches_profile_vacancy",
                )
            else:
                insert_stmt = sqlite_insert(VacancyMatch).values(rows)
                insert_stmt = insert_stmt.on_conflict_do_nothing(
                    index_elements=["search_profile_id", "vacancy_id"],
                )
            session.execute(insert_stmt)
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()
        # Re-fetch the canonical rows so the caller observes server-side
        # defaults (created_at, …) and skips rows the unique constraint
        # silently dropped.
        return self.list_by_profile_ids(
            [m.search_profile_id for m in matches],
            status=None,
        )

    # -- readers ---------------------------------------------------------

    def get_by_id(self, match_id: uuid.UUID) -> VacancyMatch | None:
        session = self._scope()
        try:
            return session.get(VacancyMatch, match_id)
        finally:
            session.close()

    def list_by_profile(
        self,
        profile_id: uuid.UUID,
        *,
        status: str | None = None,
        limit: int = 20,
    ) -> Sequence[VacancyMatch]:
        session = self._scope()
        try:
            statement = (
                select(VacancyMatch)
                .where(VacancyMatch.search_profile_id == profile_id)
                .order_by(VacancyMatch.created_at.desc())
                .limit(limit)
            )
            if status is not None:
                statement = statement.where(VacancyMatch.status == status)
            return list(session.execute(statement).scalars().all())
        finally:
            session.close()

    def list_by_user(
        self,
        user_id: uuid.UUID,
        *,
        status: str | None = None,
    ) -> Sequence[VacancyMatch]:
        session = self._scope()
        try:
            statement = (
                select(VacancyMatch)
                .join(SearchProfile, SearchProfile.id == VacancyMatch.search_profile_id)
                .where(SearchProfile.user_id == user_id)
                .order_by(VacancyMatch.created_at.desc())
            )
            if status is not None:
                statement = statement.where(VacancyMatch.status == status)
            return list(session.execute(statement).scalars().all())
        finally:
            session.close()

    def list_by_profile_ids(
        self,
        profile_ids: Sequence[uuid.UUID],
        *,
        status: str | None = None,
    ) -> Sequence[VacancyMatch]:
        """Return every match whose ``search_profile_id`` is in ``profile_ids``.

        Used by the bulk insert path to re-fetch the rows the unique
        constraint actually persisted, ignoring the ones the database
        silently dropped on conflict.
        """
        if not profile_ids:
            return []
        session = self._scope()
        try:
            statement = select(VacancyMatch).where(VacancyMatch.search_profile_id.in_(profile_ids))
            if status is not None:
                statement = statement.where(VacancyMatch.status == status)
            return list(session.execute(statement).scalars().all())
        finally:
            session.close()

    def find_existing(self, profile_id: uuid.UUID, vacancy_id: uuid.UUID) -> VacancyMatch | None:
        session = self._scope()
        try:
            statement = select(VacancyMatch).where(
                VacancyMatch.search_profile_id == profile_id,
                VacancyMatch.vacancy_id == vacancy_id,
            )
            return session.execute(statement).scalar_one_or_none()
        finally:
            session.close()


__all__ = [
    "InMemoryVacancyMatchRepository",
    "SqlVacancyMatchRepository",
    "VacancyMatchRepository",
]
