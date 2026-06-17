"""Persistence gateway for the source-metrics slice (M7, issue #62).

Three implementations live here:

* :class:`SourceMetricRepository` — the Protocol the service layer
  depends on.
* :class:`InMemorySourceMetricRepository` — list-backed fake for
  tests.
* :class:`SqlSourceMetricRepository` — production implementation
  backed by a SQLAlchemy ``Session``.

The service layer is the only writer of these events; the API layer
is the only reader. Keeping the contract as a :class:`Protocol`
makes it easy to swap in a fake (or a future async variant) without
touching the service.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Callable
from datetime import datetime
from typing import Protocol

from sqlalchemy import select
from sqlalchemy.orm import Session

from apply_pilot.features.source_metrics.models import (
    SourceMetricEvent,
    SourceMetricEventKind,
    SourceMetricEventORM,
)


class SourceMetricRepository(Protocol):
    """Minimal interface the :class:`SourceMetricsService` relies on.

    ``record`` is the only writer: the service hands the
    repository a :class:`SourceMetricEvent` and the repository is
    responsible for persisting it (in-memory list or SQL row).

    ``query`` is the only reader: it returns events for a single
    source, optionally bounded by ``since`` (strictly after) and
    ``until`` (inclusive), ordered by ``timestamp`` desc.
    """

    def record(self, event: SourceMetricEvent) -> SourceMetricEvent: ...

    def query(
        self,
        *,
        source_name: str,
        since: datetime | None,
        until: datetime | None,
    ) -> list[SourceMetricEvent]: ...


# ---------------------------------------------------------------------------
# In-memory implementation
# ---------------------------------------------------------------------------


class InMemorySourceMetricRepository:
    """List-backed repository for tests.

    Events are stored in insertion order; :meth:`query` returns them
    sorted by ``timestamp`` desc so the contract is symmetric with
    the SQL implementation. The internal list grows without bound —
    test isolation is achieved by constructing a new instance per
    test.
    """

    __slots__ = ("_events",)

    def __init__(self) -> None:
        self._events: list[SourceMetricEvent] = []

    def record(self, event: SourceMetricEvent) -> SourceMetricEvent:
        """Append *event* to the in-memory list and return it unchanged."""
        self._events.append(event)
        return event

    def query(
        self,
        *,
        source_name: str,
        since: datetime | None,
        until: datetime | None,
    ) -> list[SourceMetricEvent]:
        """Return events for *source_name* bounded by *since* and *until*.

        ``since`` is a strict lower bound (``timestamp > since``);
        ``until`` is an inclusive upper bound (``timestamp <= until``).
        The result is sorted by ``timestamp`` desc so the newest
        events surface first.
        """
        matched: list[SourceMetricEvent] = []
        for event in self._events:
            if event.source_name != source_name:
                continue
            if since is not None and not (event.timestamp > since):
                continue
            if until is not None and not (event.timestamp <= until):
                continue
            matched.append(event)
        matched.sort(key=lambda e: e.timestamp, reverse=True)
        return matched


# ---------------------------------------------------------------------------
# SQLAlchemy implementation
# ---------------------------------------------------------------------------


class SqlSourceMetricRepository:
    """SQLAlchemy-backed repository.

    Construct with either a fixed ``Session`` (caller-managed
    lifetime) or a ``session_factory`` callable (the FastAPI
    ``get_db`` pattern). Both shapes match the convention used by
    the rest of the source-metrics-adjacent slices (audit, vacancies,
    apply-jobs).
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
            raise RuntimeError("SqlSourceMetricRepository is not bound to a session")
        return self._session_factory()

    def record(self, event: SourceMetricEvent) -> SourceMetricEvent:
        """Insert a row for *event* and return the original event.

        The metadata dict is JSON-encoded into the ``metadata_json``
        column; the round-trip in :meth:`query` decodes it back.
        """
        session = self._scope()
        try:
            row = SourceMetricEventORM(
                id=event.id if event.id is not None else uuid.uuid4(),
                source_name=event.source_name,
                kind=event.kind.value,
                count=event.count,
                duration_ms=event.duration_ms,
                timestamp=event.timestamp,
                metadata_json=json.dumps(event.metadata, default=str, ensure_ascii=False),
            )
            session.add(row)
            session.commit()
            return event
        except Exception:
            session.rollback()
            raise
        finally:
            if self._session is None:
                session.close()

    def query(
        self,
        *,
        source_name: str,
        since: datetime | None,
        until: datetime | None,
    ) -> list[SourceMetricEvent]:
        """Return events for *source_name* bounded by *since* and *until*."""
        session = self._scope()
        try:
            statement = select(SourceMetricEventORM).where(
                SourceMetricEventORM.source_name == source_name
            )
            if since is not None:
                statement = statement.where(SourceMetricEventORM.timestamp > since)
            if until is not None:
                statement = statement.where(SourceMetricEventORM.timestamp <= until)
            statement = statement.order_by(SourceMetricEventORM.timestamp.desc())
            rows = list(session.execute(statement).scalars().all())
            return [
                SourceMetricEvent(
                    id=row.id,
                    source_name=row.source_name,
                    kind=SourceMetricEventKind(row.kind),
                    count=row.count,
                    duration_ms=row.duration_ms,
                    timestamp=row.timestamp,
                    metadata=json.loads(row.metadata_json) if row.metadata_json else {},
                )
                for row in rows
            ]
        finally:
            if self._session is None:
                session.close()


__all__ = [
    "InMemorySourceMetricRepository",
    "SourceMetricRepository",
    "SqlSourceMetricRepository",
]
