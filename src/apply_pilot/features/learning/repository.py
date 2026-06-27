"""Persistence gateway for the learning-signals slice (M8, issue #63).

Three implementations live here, mirroring the convention used by
the audit and matches slices:

* :class:`LearningSignalRepository` — :class:`typing.Protocol`
  defining the contract the service layer depends on.
* :class:`InMemoryLearningSignalRepository` — list-backed fake for
  tests.
* :class:`SqlLearningSignalRepository` — production implementation
  backed by a SQLAlchemy ``Session``.

The value object the repository accepts and returns —
:class:`LearningSignal` — is re-exported from
:mod:`apply_pilot.features.learning.service` for convenience.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Callable, Sequence
from datetime import datetime
from typing import Protocol, runtime_checkable

from sqlalchemy import select
from sqlalchemy.orm import Session

from apply_pilot.features.learning.models import LearningSignal, LearningSignalRow

_LOGGER = logging.getLogger("apply_pilot.features.learning.repository")


# ---------------------------------------------------------------------------
# Protocol
# ---------------------------------------------------------------------------


@runtime_checkable
class LearningSignalRepository(Protocol):
    """Minimal contract :class:`LearningSignalsService` relies on.

    * :meth:`record` — persist a single :class:`LearningSignal`.
    * :meth:`list_for_user` — read a user's signals, newest first,
      capped at ``limit``.
    * :meth:`list_for_prompt` — read signals for a single prompt
      version since ``since``, oldest first, so the future
      prompt-tuning pipeline can stream them chronologically.
    """

    def record(self, signal: LearningSignal) -> LearningSignal: ...
    def list_for_user(
        self, user_id: uuid.UUID, *, limit: int = 100
    ) -> Sequence[LearningSignal]: ...
    def list_for_prompt(
        self, prompt_version: str, *, since: datetime
    ) -> Sequence[LearningSignal]: ...


# ---------------------------------------------------------------------------
# In-memory implementation
# ---------------------------------------------------------------------------


class InMemoryLearningSignalRepository:
    """List-backed repository for tests.

    ``record`` stores the signal as-is; ``list_for_user`` returns the
    matching signals ordered by ``created_at`` descending, capped at
    ``limit``; ``list_for_prompt`` returns the matching signals
    ordered by ``created_at`` ascending so the caller can stream
    them chronologically when feeding the future tuning pipeline.
    """

    __slots__ = ("_signals",)

    def __init__(self) -> None:
        self._signals: list[LearningSignal] = []

    def record(self, signal: LearningSignal) -> LearningSignal:
        self._signals.append(signal)
        return signal

    def list_for_user(self, user_id: uuid.UUID, *, limit: int = 100) -> list[LearningSignal]:
        matching = [s for s in self._signals if s.user_id == user_id]
        matching.sort(key=lambda s: s.created_at, reverse=True)
        return matching[:limit]

    def list_for_prompt(self, prompt_version: str, *, since: datetime) -> list[LearningSignal]:
        matching = [
            s for s in self._signals if s.prompt_version == prompt_version and s.created_at >= since
        ]
        matching.sort(key=lambda s: s.created_at)
        return matching


# ---------------------------------------------------------------------------
# SQLAlchemy implementation
# ---------------------------------------------------------------------------


def _row_to_signal(row: LearningSignalRow) -> LearningSignal:
    """Translate a :class:`LearningSignalRow` ORM row into the value object."""
    return LearningSignal(
        id=row.id,
        user_id=row.user_id,
        match_id=row.match_id,
        vacancy_id=row.vacancy_id,
        search_profile_id=row.search_profile_id,
        rejection_reason=row.rejection_reason,
        prompt_version=row.prompt_version,
        score=row.score,
        signal_type=row.signal_type,  # type: ignore[invalid-argument-type]
        created_at=row.created_at,
    )


class SqlLearningSignalRepository:
    """SQLAlchemy-backed repository.

    The repository can be constructed two ways:

    * With a single ``Session`` (caller-managed lifetime). Useful for
      FastAPI's per-request ``get_db``.
    * With a ``session_factory`` (default). The repository opens a
      short-lived session per operation and closes it before
      returning.
    """

    __slots__ = ("_session", "_session_factory")

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
            raise RuntimeError("SqlLearningSignalRepository is not bound to a session")
        return self._session_factory()

    def record(self, signal: LearningSignal) -> LearningSignal:
        session = self._scope()
        try:
            row = LearningSignalRow(
                id=signal.id,
                user_id=signal.user_id,
                match_id=signal.match_id,
                vacancy_id=signal.vacancy_id,
                search_profile_id=signal.search_profile_id,
                rejection_reason=signal.rejection_reason,
                prompt_version=signal.prompt_version,
                score=signal.score,
                signal_type=signal.signal_type,
                created_at=signal.created_at,
            )
            session.add(row)
            session.commit()
            session.refresh(row)
            return _row_to_signal(row)
        except Exception:
            session.rollback()
            raise
        finally:
            if self._session is None:
                session.close()

    def list_for_user(self, user_id: uuid.UUID, *, limit: int = 100) -> list[LearningSignal]:
        session = self._scope()
        try:
            statement = (
                select(LearningSignalRow)
                .where(LearningSignalRow.user_id == user_id)
                .order_by(LearningSignalRow.created_at.desc())
                .limit(limit)
            )
            rows: Sequence[LearningSignalRow] = list(session.execute(statement).scalars().all())
            return [_row_to_signal(r) for r in rows]
        finally:
            if self._session is None:
                session.close()

    def list_for_prompt(self, prompt_version: str, *, since: datetime) -> list[LearningSignal]:
        session = self._scope()
        try:
            statement = (
                select(LearningSignalRow)
                .where(
                    LearningSignalRow.prompt_version == prompt_version,
                    LearningSignalRow.created_at >= since,
                )
                .order_by(LearningSignalRow.created_at.asc())
            )
            rows: Sequence[LearningSignalRow] = list(session.execute(statement).scalars().all())
            return [_row_to_signal(r) for r in rows]
        finally:
            if self._session is None:
                session.close()


__all__ = [
    "InMemoryLearningSignalRepository",
    "LearningSignalRepository",
    "SqlLearningSignalRepository",
]
