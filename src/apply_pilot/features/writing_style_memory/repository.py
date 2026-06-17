"""Persistence gateway for the ``writing_style_memory`` slice.

Three implementations live here, mirroring the convention used by the
``cover_letter_style`` and ``cover_letter`` slices:

* :class:`StyleMemoryRepository` — Protocol defining the contract
  the service layer depends on.
* :class:`InMemoryStyleMemoryRepository` — list-backed fake for
  tests.
* :class:`SqlStyleMemoryRepository` — production implementation
  backed by a SQLAlchemy ``Session``.

Design notes
------------

* The slice is append-only: every accepted cover letter produces a
  fresh :class:`StyleMemoryEntry`. There is no ``update`` or
  ``delete`` on the read side yet — the issue contract is a "memory",
  not a "single style row". The repository's contract is therefore
  intentionally tiny.
* ``list_for_user`` orders entries newest-first so the API can show
  the most recent style influence at the top of the read-back.
* ``get_aggregated`` is the cheap read used by the API: it concatenates
  the recent entries' ``style_summary`` strings, newest first, with
  a blank line between them. The aggregation is intentionally simple
  — LLM-based roll-up is a follow-up.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from typing import Protocol, runtime_checkable

from sqlalchemy import select
from sqlalchemy.orm import Session

from apply_pilot.features.writing_style_memory.models import (
    StyleMemoryEntry,
    StyleMemoryEntryModel,
)

# Default cap on the number of entries returned in a single read. The
# API uses this number for both ``list_for_user`` and
# ``get_aggregated``; bump it only when the summary column is no
# longer a viable single-response payload.
DEFAULT_AGGREGATED_LIMIT = 10


@runtime_checkable
class StyleMemoryRepository(Protocol):
    """Minimal interface :class:`StyleMemoryService` relies on."""

    def record(
        self,
        *,
        user_id: uuid.UUID,
        cover_letter_id: uuid.UUID | None,
        letter_text: str,
        style_summary: str,
    ) -> StyleMemoryEntry: ...

    def list_for_user(
        self, user_id: uuid.UUID, *, limit: int = DEFAULT_AGGREGATED_LIMIT
    ) -> Sequence[StyleMemoryEntry]: ...

    def get_aggregated(
        self, user_id: uuid.UUID, *, limit: int = DEFAULT_AGGREGATED_LIMIT
    ) -> str | None: ...


# ---------------------------------------------------------------------------
# In-memory implementation
# ---------------------------------------------------------------------------


class InMemoryStyleMemoryRepository:
    """List-backed repository for tests.

    The list is the source of truth; ``list_for_user`` filters it
    through a simple comprehension. New entries get a fresh
    ``uuid.uuid4`` id and a UTC ``created_at`` timestamp so the
    in-memory store mirrors the SQL default timestamps the production
    code relies on.
    """

    def __init__(self) -> None:
        self._entries: list[StyleMemoryEntry] = []

    def record(
        self,
        *,
        user_id: uuid.UUID,
        cover_letter_id: uuid.UUID | None,
        letter_text: str,
        style_summary: str,
    ) -> StyleMemoryEntry:
        entry = StyleMemoryEntry(
            id=uuid.uuid4(),
            user_id=user_id,
            cover_letter_id=cover_letter_id,
            letter_text=letter_text,
            style_summary=style_summary,
            created_at=datetime.now(UTC),
        )
        self._entries.append(entry)
        return entry

    def list_for_user(
        self, user_id: uuid.UUID, *, limit: int = DEFAULT_AGGREGATED_LIMIT
    ) -> Sequence[StyleMemoryEntry]:
        matched = [e for e in self._entries if e.user_id == user_id]
        # Newest first; tie-break on id (UUID lex order) for determinism.
        matched.sort(key=lambda e: (e.created_at, e.id), reverse=True)
        return matched[:limit]

    def get_aggregated(
        self, user_id: uuid.UUID, *, limit: int = DEFAULT_AGGREGATED_LIMIT
    ) -> str | None:
        matched = self.list_for_user(user_id, limit=limit)
        if not matched:
            return None
        return "\n\n".join(entry.style_summary for entry in matched)


# ---------------------------------------------------------------------------
# SQLAlchemy implementation
# ---------------------------------------------------------------------------


class SqlStyleMemoryRepository:
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
            raise RuntimeError("SqlStyleMemoryRepository requires a Session or session_factory")
        self._session = session
        self._session_factory = session_factory

    def _scope(self) -> Session:
        if self._session is not None:
            return self._session
        if self._session_factory is None:
            raise RuntimeError("SqlStyleMemoryRepository is not bound to a session")
        return self._session_factory()

    def _close_if_ephemeral(self, session: Session) -> None:
        if self._session is None:
            session.close()

    @staticmethod
    def _to_dto(row: StyleMemoryEntryModel) -> StyleMemoryEntry:
        """Map an ORM row to the frozen DTO the service exchanges."""
        return StyleMemoryEntry(
            id=row.id,
            user_id=row.user_id,
            cover_letter_id=row.cover_letter_id,
            letter_text=row.letter_text,
            style_summary=row.style_summary,
            created_at=row.created_at,
        )

    def record(
        self,
        *,
        user_id: uuid.UUID,
        cover_letter_id: uuid.UUID | None,
        letter_text: str,
        style_summary: str,
    ) -> StyleMemoryEntry:
        session = self._scope()
        try:
            row = StyleMemoryEntryModel(
                user_id=user_id,
                cover_letter_id=cover_letter_id,
                letter_text=letter_text,
                style_summary=style_summary,
            )
            session.add(row)
            session.commit()
            session.refresh(row)
            return self._to_dto(row)
        except Exception:
            session.rollback()
            raise
        finally:
            self._close_if_ephemeral(session)

    def list_for_user(
        self, user_id: uuid.UUID, *, limit: int = DEFAULT_AGGREGATED_LIMIT
    ) -> Sequence[StyleMemoryEntry]:
        session = self._scope()
        try:
            statement = (
                select(StyleMemoryEntryModel)
                .where(StyleMemoryEntryModel.user_id == user_id)
                .order_by(
                    StyleMemoryEntryModel.created_at.desc(),
                    StyleMemoryEntryModel.id.desc(),
                )
                .limit(limit)
            )
            rows = list(session.scalars(statement).all())
            return [self._to_dto(row) for row in rows]
        finally:
            self._close_if_ephemeral(session)

    def get_aggregated(
        self, user_id: uuid.UUID, *, limit: int = DEFAULT_AGGREGATED_LIMIT
    ) -> str | None:
        entries = self.list_for_user(user_id, limit=limit)
        if not entries:
            return None
        return "\n\n".join(entry.style_summary for entry in entries)


__all__ = [
    "DEFAULT_AGGREGATED_LIMIT",
    "InMemoryStyleMemoryRepository",
    "SqlStyleMemoryRepository",
    "StyleMemoryRepository",
]
