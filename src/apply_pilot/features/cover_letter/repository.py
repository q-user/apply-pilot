"""Persistence gateway for the ``cover_letter`` slice (M3, issue #31).

Three implementations live here, mirroring the convention used by the
``cover_letter_style`` and ``matches`` slices:

* :class:`CoverLetterDraftRepository` — Protocol defining the contract
  the service layer depends on.
* :class:`InMemoryCoverLetterDraftRepository` — dict-backed fake for
  tests.
* :class:`SqlCoverLetterDraftRepository` — production implementation
  backed by a SQLAlchemy ``Session``.

The ``match_id`` UNIQUE constraint is the M3 #31 contract: one draft
per match. The follow-up issue (#32) introduces the version-history
workflow that lifts this constraint; until then, the service is
expected to upsert the existing row's ``content`` on repeat calls.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from typing import Protocol, runtime_checkable

from sqlalchemy import select
from sqlalchemy.orm import Session

from apply_pilot.features.cover_letter.models import CoverLetterDraft


@runtime_checkable
class CoverLetterDraftRepository(Protocol):
    """Minimal interface :class:`CoverLetterService` relies on.

    The slice is intentionally tiny:

    * :meth:`create` — insert the first (and only, under #31) draft.
    * :meth:`get_by_match` — fetch the single draft for a match.
    * :meth:`get_by_id` — fetch a draft by its primary key.
    * :meth:`list_by_user` — list drafts owned by a user, optionally
      filtered by status.
    * :meth:`update_status` — move a draft through the lifecycle
      (e.g. ``draft`` → ``final`` → ``sent``).
    """

    def create(self, draft: CoverLetterDraft) -> CoverLetterDraft: ...
    def get_by_match(self, match_id: uuid.UUID) -> CoverLetterDraft | None: ...
    def get_by_id(self, draft_id: uuid.UUID) -> CoverLetterDraft | None: ...
    def list_by_user(
        self,
        user_id: uuid.UUID,
        *,
        status: str | None = None,
        limit: int = 20,
    ) -> Sequence[CoverLetterDraft]: ...
    def update_content(
        self,
        match_id: uuid.UUID,
        content: str,
        prompt_version: str,
        model_used: str | None,
    ) -> CoverLetterDraft | None: ...
    def update_status(self, draft_id: uuid.UUID, status: str) -> CoverLetterDraft: ...


# ---------------------------------------------------------------------------
# In-memory implementation
# ---------------------------------------------------------------------------


class InMemoryCoverLetterDraftRepository:
    """Dict-backed repository for tests.

    Two indices back the read paths:

    * ``_by_id`` — primary key lookup.
    * ``_by_match`` — match → draft id, used by :meth:`get_by_match`.

    ``list_by_user`` scans the in-memory store and filters by the
    supplied ``user_id`` / ``status``. The result is ordered by
    ``created_at`` descending (newest first) so the behaviour matches
    the SQL implementation.
    """

    def __init__(self) -> None:
        self._by_id: dict[uuid.UUID, CoverLetterDraft] = {}
        self._by_match: dict[uuid.UUID, uuid.UUID] = {}

    # -- writers --------------------------------------------------------

    def create(self, draft: CoverLetterDraft) -> CoverLetterDraft:
        if draft.id is None:
            draft.id = uuid.uuid4()
        if draft.created_at is None:
            draft.created_at = datetime.now(UTC)
        # The SQL default is ``"draft"``; the in-memory repo mirrors it
        # so ``create`` produces a row with the same status the SQL
        # insert would. Without this, an in-memory unit test that
        # doesn't set ``status`` would see ``None`` and diverge from
        # the production behaviour.
        if draft.status is None:
            draft.status = "draft"
        # Mirror the SQL ``server_default="1"`` on the ``version``
        # column so an in-memory draft created without an explicit
        # version reads back as ``1`` — same shape as a fresh SQL
        # row. Added in M4 issue #40 (``/regenerate``).
        if draft.version is None:
            draft.version = 1
        self._by_id[draft.id] = draft
        self._by_match[draft.match_id] = draft.id
        return draft

    def update_status(self, draft_id: uuid.UUID, status: str) -> CoverLetterDraft:
        existing = self._by_id.get(draft_id)
        if existing is None:
            raise KeyError(f"cover letter draft {draft_id} not found")
        existing.status = status
        existing.updated_at = datetime.now(UTC)
        return existing

    def update_content(
        self,
        match_id: uuid.UUID,
        content: str,
        prompt_version: str,
        model_used: str | None,
    ) -> CoverLetterDraft | None:
        """Update ``content`` / ``prompt_version`` / ``model_used`` for the
        draft bound to ``match_id``.

        Returns the updated draft, or ``None`` when no draft exists for
        that match. Issue #144 — the previous service-side mutation
        path was a no-op against the SQL repo because ``get_by_match``
        returns a detached instance.
        """
        draft = self.get_by_match(match_id)
        if draft is None:
            return None
        draft.content = content
        draft.prompt_version = prompt_version
        draft.model_used = model_used
        draft.updated_at = datetime.now(UTC)
        return draft

    # -- readers --------------------------------------------------------

    def get_by_id(self, draft_id: uuid.UUID) -> CoverLetterDraft | None:
        return self._by_id.get(draft_id)

    def get_by_match(self, match_id: uuid.UUID) -> CoverLetterDraft | None:
        draft_id = self._by_match.get(match_id)
        if draft_id is None:
            return None
        return self._by_id.get(draft_id)

    def list_by_user(
        self,
        user_id: uuid.UUID,
        *,
        status: str | None = None,
        limit: int = 20,
    ) -> Sequence[CoverLetterDraft]:
        drafts = [d for d in self._by_id.values() if d.user_id == user_id]
        if status is not None:
            drafts = [d for d in drafts if d.status == status]
        drafts.sort(key=lambda d: (d.created_at, d.id), reverse=True)
        return drafts[:limit]


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

    def _close_if_ephemeral(self, session: Session) -> None:
        if self._session is None:
            session.close()

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
            self._close_if_ephemeral(session)

    def update_status(self, draft_id: uuid.UUID, status: str) -> CoverLetterDraft:
        session = self._scope()
        try:
            existing = session.get(CoverLetterDraft, draft_id)
            if existing is None:
                raise KeyError(f"cover letter draft {draft_id} not found")
            existing.status = status
            session.commit()
            session.refresh(existing)
            return existing
        except Exception:
            session.rollback()
            raise
        finally:
            self._close_if_ephemeral(session)

    def update_content(
        self,
        match_id: uuid.UUID,
        content: str,
        prompt_version: str,
        model_used: str | None,
    ) -> CoverLetterDraft | None:
        """Replace ``content`` / ``prompt_version`` / ``model_used`` on
        the draft bound to ``match_id`` and commit.

        Issue #144 — the service used to mutate the ORM instance
        returned by :meth:`get_by_match`, but the SQL repo closes its
        session inside the call, leaving a detached instance whose
        attribute writes are silently lost. This method re-fetches the
        row in a fresh session, mutates it, and commits, so the change
        is durable.

        Returns the refreshed draft, or ``None`` when no draft exists
        for ``match_id``.
        """
        session = self._scope()
        try:
            statement = select(CoverLetterDraft).where(CoverLetterDraft.match_id == match_id)
            existing = session.scalars(statement).first()
            if existing is None:
                return None
            existing.content = content
            existing.prompt_version = prompt_version
            existing.model_used = model_used
            existing.updated_at = datetime.now(UTC)
            session.commit()
            session.refresh(existing)
            return existing
        except Exception:
            session.rollback()
            raise
        finally:
            self._close_if_ephemeral(session)

    # -- readers --------------------------------------------------------

    def get_by_id(self, draft_id: uuid.UUID) -> CoverLetterDraft | None:
        session = self._scope()
        try:
            return session.get(CoverLetterDraft, draft_id)
        finally:
            self._close_if_ephemeral(session)

    def get_by_match(self, match_id: uuid.UUID) -> CoverLetterDraft | None:
        session = self._scope()
        try:
            statement = select(CoverLetterDraft).where(CoverLetterDraft.match_id == match_id)
            return session.scalars(statement).first()
        finally:
            self._close_if_ephemeral(session)

    def list_by_user(
        self,
        user_id: uuid.UUID,
        *,
        status: str | None = None,
        limit: int = 20,
    ) -> Sequence[CoverLetterDraft]:
        session = self._scope()
        try:
            statement = (
                select(CoverLetterDraft)
                .where(CoverLetterDraft.user_id == user_id)
                .order_by(CoverLetterDraft.created_at.desc(), CoverLetterDraft.id.desc())
                .limit(limit)
            )
            if status is not None:
                statement = statement.where(CoverLetterDraft.status == status)
            return list(session.scalars(statement).all())
        finally:
            self._close_if_ephemeral(session)


__all__ = [
    "CoverLetterDraftRepository",
    "InMemoryCoverLetterDraftRepository",
    "SqlCoverLetterDraftRepository",
]
