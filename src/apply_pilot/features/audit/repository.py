"""Persistence gateway for the audit slice.

Three implementations:

* ``AuditLogRepository`` — Protocol defining the contract.
* ``SqlAuditLogRepository`` — production implementation backed by
  a SQLAlchemy ``Session``.
* ``InMemoryAuditLogRepository`` — list-backed fake for tests.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from typing import Protocol

from sqlalchemy import select
from sqlalchemy.orm import Session

from apply_pilot.features.audit.models import AuditLog


class AuditLogRepository(Protocol):
    """Minimal interface consumed by ``AuditService``."""

    def insert(
        self, *, event_type: str, user_id: uuid.UUID | None, details: str | None
    ) -> AuditLog: ...

    def list_by_user(self, user_id: uuid.UUID) -> list[AuditLog]: ...

    def list_by_event_type(self, event_type: str) -> list[AuditLog]: ...

    def list_recent(self, limit: int) -> list[AuditLog]: ...


# ---------------------------------------------------------------------------
# In-memory implementation (for tests)
# ---------------------------------------------------------------------------


class InMemoryAuditLogRepository:
    """List-backed repository for unit tests.

    Every insert returns a new ``AuditLog`` with a generated id and timestamp.
    The internal list grows without bound — test isolation is achieved by
    constructing a new instance per test.
    """

    def __init__(self) -> None:
        self._logs: list[AuditLog] = []

    def insert(
        self,
        *,
        event_type: str,
        user_id: uuid.UUID | None,
        details: str | None,
    ) -> AuditLog:
        log = AuditLog(
            id=uuid.uuid4(),
            event_type=event_type,
            user_id=user_id,
            details=details,
        )
        from datetime import UTC, datetime

        log.created_at = datetime.now(UTC)
        self._logs.append(log)
        return log

    def list_by_user(self, user_id: uuid.UUID) -> list[AuditLog]:
        return [log for log in self._logs if log.user_id == user_id]

    def list_by_event_type(self, event_type: str) -> list[AuditLog]:
        return [log for log in self._logs if log.event_type == event_type]

    def list_recent(self, limit: int) -> list[AuditLog]:
        return sorted(self._logs, key=lambda log: log.created_at, reverse=True)[:limit]


# ---------------------------------------------------------------------------
# SQLAlchemy implementation
# ---------------------------------------------------------------------------


class SqlAuditLogRepository:
    """SQLAlchemy-backed repository.

    The repository can be constructed two ways:

    * With a single ``Session`` (caller-managed lifetime). Useful for
      FastAPI's per-request ``get_db``.
    * With a ``session_factory`` (default). The repository opens a
      short-lived session per operation and closes it before returning.
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
            raise RuntimeError("SqlAuditLogRepository is not bound to a session")
        return self._session_factory()

    def insert(
        self,
        *,
        event_type: str,
        user_id: uuid.UUID | None,
        details: str | None,
    ) -> AuditLog:
        session = self._scope()
        try:
            log = AuditLog(
                event_type=event_type,
                user_id=user_id,
                details=details,
            )
            session.add(log)
            session.commit()
            session.refresh(log)
            return log
        except Exception:
            session.rollback()
            raise
        finally:
            if self._session is None:
                session.close()

    def list_by_user(self, user_id: uuid.UUID) -> list[AuditLog]:
        session = self._scope()
        try:
            statement = select(AuditLog).where(AuditLog.user_id == user_id)
            return list(session.execute(statement).scalars().all())
        finally:
            if self._session is None:
                session.close()

    def list_by_event_type(self, event_type: str) -> list[AuditLog]:
        session = self._scope()
        try:
            statement = select(AuditLog).where(AuditLog.event_type == event_type)
            return list(session.execute(statement).scalars().all())
        finally:
            if self._session is None:
                session.close()

    def list_recent(self, limit: int) -> list[AuditLog]:
        session = self._scope()
        try:
            statement = select(AuditLog).order_by(AuditLog.created_at.desc()).limit(limit)
            return list(session.execute(statement).scalars().all())
        finally:
            if self._session is None:
                session.close()


__all__ = [
    "AuditLogRepository",
    "InMemoryAuditLogRepository",
    "SqlAuditLogRepository",
]
