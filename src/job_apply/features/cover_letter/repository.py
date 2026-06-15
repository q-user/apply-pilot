"""Persistence gateway for the ``cover_letter`` slice (M3, issue #32).

Three implementations live here, mirroring the convention used by the
``cover_letter_style`` and ``matches`` slices:

* :class:`CoverLetterDraftRepository` — Protocol defining the contract
  the service layer depends on.
* :class:`InMemoryCoverLetterDraftRepository` — dict-backed fake for
  tests.
* :class:`SqlCoverLetterDraftRepository` — production implementation
  backed by a SQLAlchemy ``Session``.

History semantics
-----------------

The repository owns three read paths that the service relies on:

* :meth:`get_latest_for_match` — returns the highest-``version`` draft
  for a match, or ``None`` if there is none.
* :meth:`list_by_match` — returns every draft for a match, ordered by
  ``version`` descending (newest first).
* :meth:`get_by_match_and_version` — returns the specific draft for a
  given ``(match_id, version)`` pair, used by the service to
  back-link a new draft to its parent.

The two write paths are :meth:`create` (used for the very first draft
and for every regeneration) and :meth:`update_replaced_by` (used after
a regeneration to point the previous draft at its successor). The
:func:`compute_prompt_hash` helper from the generator module is the
only place that knows how to derive the audit hash.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from typing import Protocol, runtime_checkable

from sqlalchemy import select
from sqlalchemy.orm import Session

from job_apply.features.cover_letter.models import CoverLetterDraft


@runtime_checkable
class CoverLetterDraftRepository(Protocol):
    """Minimal interface :class:`CoverLetterService` relies on.

    Read methods take match and draft ids as plain UUIDs. Write methods
    accept fully-constructed ORM rows; the service is the only place
    that decides which fields to populate.
    """

    def create(self, draft: CoverLetterDraft) -> CoverLetterDraft: ...
    def get_by_id(self, draft_id: uuid.UUID) -> CoverLetterDraft | None: ...
    def get_by_match_and_version(
        self, match_id: uuid.UUID, version: int
    ) -> CoverLetterDraft | None: ...
    def get_latest_for_match(self, match_id: uuid.UUID) -> CoverLetterDraft | None: ...
    def list_by_match(self, match_id: uuid.UUID) -> Sequence[CoverLetterDraft]: ...
    def update_replaced_by(
        self, draft_id: uuid.UUID, replaced_by_id: uuid.UUID
    ) -> CoverLetterDraft: ...


# ---------------------------------------------------------------------------
# In-memory implementation
# ---------------------------------------------------------------------------


class InMemoryCoverLetterDraftRepository:
    """Dict-backed repository for tests.

    Stores drafts in a single ``_by_id`` dict plus a ``_by_match`` list
    so the history queries can be answered without a full scan. The
    list keeps drafts in insertion order, and the read methods sort by
    ``version`` descending so the behaviour matches the SQL
    implementation regardless of insertion order.
    """

    def __init__(self) -> None:
        self._by_id: dict[uuid.UUID, CoverLetterDraft] = {}
        self._by_match: dict[uuid.UUID, list[uuid.UUID]] = {}

    # -- writers --------------------------------------------------------

    def create(self, draft: CoverLetterDraft) -> CoverLetterDraft:
        if draft.id is None:
            draft.id = uuid.uuid4()
        if draft.created_at is None:
            draft.created_at = datetime.now(UTC)
        self._by_id[draft.id] = draft
        self._by_match.setdefault(draft.match_id, []).append(draft.id)
        return draft

    def update_replaced_by(
        self, draft_id: uuid.UUID, replaced_by_id: uuid.UUID
    ) -> CoverLetterDraft:
        existing = self._by_id.get(draft_id)
        if existing is None:
            raise KeyError(f"cover letter draft {draft_id} not found")
        existing.replaced_by_id = replaced_by_id
        existing.updated_at = datetime.now(UTC)
        return existing

    # -- readers --------------------------------------------------------

    def get_by_id(self, draft_id: uuid.UUID) -> CoverLetterDraft | None:
        return self._by_id.get(draft_id)

    def get_by_match_and_version(
        self, match_id: uuid.UUID, version: int
    ) -> CoverLetterDraft | None:
        for draft_id in self._by_match.get(match_id, []):
            draft = self._by_id.get(draft_id)
            if draft is not None and draft.version == version:
                return draft
        return None

    def get_latest_for_match(self, match_id: uuid.UUID) -> CoverLetterDraft | None:
        drafts = list(self.list_by_match(match_id))
        return drafts[0] if drafts else None

    def list_by_match(self, match_id: uuid.UUID) -> Sequence[CoverLetterDraft]:
        ids = self._by_match.get(match_id, [])
        drafts = [self._by_id[i] for i in ids if i in self._by_id]
        drafts.sort(key=lambda d: d.version, reverse=True)
        return drafts


# ---------------------------------------------------------------------------
# SQLAlchemy implementation
# ---------------------------------------------------------------------------


class SqlCoverLetterDraftRepository:
    """SQLAlchemy-backed repository.

    Construct with either a fixed ``Session`` (caller-managed lifetime)
    or a ``session_factory`` callable (the FastAPI ``get_db`` pattern).
    Each operation opens a short-lived session unless a fixed session
    was supplied.
    """

    def __init__(
        self,
        *,
        session: Session | None = None,
        session_factory: Callable[[], Session] | None = None,
    ) -> None:
        if session is None and session_factory is None:
            raise RuntimeError(
                "SqlCoverLetterDraftRepository requires a Session or session_factory"
            )
        self._session = session
        self._session_factory = session_factory

    def _scope(self) -> Session:
        if self._session is not None:
            return self._session
        if self._session_factory is None:
            raise RuntimeError("SqlCoverLetterDraftRepository is not bound to a session")
        return self._session_factory()

    # -- writers --------------------------------------------------------

    def create(self, draft: CoverLetterDraft) -> CoverLetterDraft:
        session = self._scope()
        try:
            session.add(draft)
            session.commit()
            session.refresh(draft)
            return draft
        except Exception:
            session.rollback()
            raise
        finally:
            if self._session_factory is not None:
                session.close()

    def update_replaced_by(
        self, draft_id: uuid.UUID, replaced_by_id: uuid.UUID
    ) -> CoverLetterDraft:
        session = self._scope()
        try:
            existing = session.get(CoverLetterDraft, draft_id)
            if existing is None:
                raise KeyError(f"cover letter draft {draft_id} not found")
            existing.replaced_by_id = replaced_by_id
            session.commit()
            session.refresh(existing)
            return existing
        except Exception:
            session.rollback()
            raise
        finally:
            if self._session_factory is not None:
                session.close()

    # -- readers --------------------------------------------------------

    def get_by_id(self, draft_id: uuid.UUID) -> CoverLetterDraft | None:
        session = self._scope()
        try:
            return session.get(CoverLetterDraft, draft_id)
        finally:
            if self._session_factory is not None:
                session.close()

    def get_by_match_and_version(
        self, match_id: uuid.UUID, version: int
    ) -> CoverLetterDraft | None:
        session = self._scope()
        try:
            statement = (
                select(CoverLetterDraft)
                .where(
                    CoverLetterDraft.match_id == match_id,
                    CoverLetterDraft.version == version,
                )
                .limit(1)
            )
            return session.execute(statement).scalar_one_or_none()
        finally:
            if self._session_factory is not None:
                session.close()

    def get_latest_for_match(self, match_id: uuid.UUID) -> CoverLetterDraft | None:
        session = self._scope()
        try:
            statement = (
                select(CoverLetterDraft)
                .where(CoverLetterDraft.match_id == match_id)
                .order_by(CoverLetterDraft.version.desc())
                .limit(1)
            )
            return session.execute(statement).scalar_one_or_none()
        finally:
            if self._session_factory is not None:
                session.close()

    def list_by_match(self, match_id: uuid.UUID) -> Sequence[CoverLetterDraft]:
        session = self._scope()
        try:
            statement = (
                select(CoverLetterDraft)
                .where(CoverLetterDraft.match_id == match_id)
                .order_by(CoverLetterDraft.version.desc())
            )
            return list(session.execute(statement).scalars().all())
        finally:
            if self._session_factory is not None:
                session.close()


__all__ = [
    "CoverLetterDraftRepository",
    "InMemoryCoverLetterDraftRepository",
    "SqlCoverLetterDraftRepository",
]
