"""TDD tests for :class:`SqlSourceMetricRepository`.

The SQL repository is a thin wrapper around the
:class:`SourceMetricEventORM` table. The tests stand up an in-memory
sqlite engine with the same ``Base.metadata`` used in production, so
every column and index in the model is exercised by the test run.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import StaticPool, create_engine, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from apply_pilot.db import Base
from apply_pilot.features.source_metrics import models as _sm_models  # noqa: F401
from apply_pilot.features.source_metrics.models import (
    SourceMetricEvent,
    SourceMetricEventKind,
)
from apply_pilot.features.source_metrics.repository import SqlSourceMetricRepository


@pytest.fixture
def engine() -> Iterator[Engine]:
    """Fresh in-memory sqlite engine per test with all tables created."""
    eng = create_engine(
        "sqlite:///:memory:",
        future=True,
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=eng)
    try:
        yield eng
    finally:
        eng.dispose()


@pytest.fixture
def session_factory(engine: Engine) -> sessionmaker[Session]:
    """Bind a session factory to the in-memory engine."""
    return sessionmaker(bind=engine, class_=Session, autocommit=False, autoflush=False)


@pytest.fixture
def repo(session_factory: sessionmaker[Session]) -> SqlSourceMetricRepository:
    """SQL repo bound to the per-test session factory."""
    return SqlSourceMetricRepository(session_factory=session_factory)


def _make(
    *,
    source: str = "hh",
    kind: SourceMetricEventKind = SourceMetricEventKind.FETCH,
    count: int = 1,
    duration_ms: int = 10,
    timestamp: datetime | None = None,
    metadata: dict | None = None,
) -> SourceMetricEvent:
    """Build a :class:`SourceMetricEvent` with sensible test defaults."""
    return SourceMetricEvent(
        id=uuid.uuid4(),
        source_name=source,
        kind=kind,
        count=count,
        duration_ms=duration_ms,
        timestamp=timestamp or datetime.now(UTC),
        metadata=metadata or {},
    )


def test_record_inserts_row(repo: SqlSourceMetricRepository, engine: Engine) -> None:
    """``record`` must insert a row visible via a direct ``SELECT``."""
    event = _make(source="hh", count=5, duration_ms=120, metadata={"k": "v"})
    repo.record(event)

    with engine.connect() as conn:
        result = conn.execute(
            text(
                "SELECT id, source_name, kind, count, duration_ms, metadata_json "
                "FROM source_metric_events WHERE id = :id"
            ),
            {"id": str(event.id)},
        )
        row = result.mappings().one()
    assert row["source_name"] == "hh"
    assert row["kind"] == "fetch"
    assert row["count"] == 5
    assert row["duration_ms"] == 120
    assert json.loads(row["metadata_json"]) == {"k": "v"}


def test_record_round_trip_metadata(repo: SqlSourceMetricRepository) -> None:
    """The metadata dict must survive a SQLite round-trip (json.loads)."""
    event = _make(metadata={"failures": 3, "tags": ["timeout", "5xx"]})
    saved = repo.record(event)
    fetched = repo.query(source_name="hh", since=None, until=None)
    assert len(fetched) == 1
    assert fetched[0].id == saved.id
    assert fetched[0].metadata == {"failures": 3, "tags": ["timeout", "5xx"]}


def test_query_filters_by_source_name(repo: SqlSourceMetricRepository) -> None:
    """``query`` must restrict to the requested source only."""
    repo.record(_make(source="hh"))
    repo.record(_make(source="habr"))
    repo.record(_make(source="hh"))

    hh = repo.query(source_name="hh", since=None, until=None)
    assert len(hh) == 2
    assert {e.source_name for e in hh} == {"hh"}


def test_query_filters_by_since_strict(repo: SqlSourceMetricRepository) -> None:
    """``since`` is a strict lower bound — equal timestamps are excluded."""
    old = datetime(2026, 1, 1, tzinfo=UTC)
    recent = datetime(2026, 6, 1, tzinfo=UTC)
    cutoff = datetime(2026, 3, 1, tzinfo=UTC)

    repo.record(_make(timestamp=old))
    repo.record(_make(timestamp=recent))

    after = repo.query(source_name="hh", since=cutoff, until=None)
    assert len(after) == 1
    # sqlite drops tzinfo on round-trip; compare naive values.
    assert after[0].timestamp.replace(tzinfo=None) == recent.replace(tzinfo=None)


def test_query_filters_by_until_inclusive(repo: SqlSourceMetricRepository) -> None:
    """``until`` is an inclusive upper bound — equal timestamps are included."""
    early = datetime(2026, 1, 1, tzinfo=UTC)
    late = datetime(2026, 6, 1, tzinfo=UTC)

    repo.record(_make(timestamp=early))
    repo.record(_make(timestamp=late))

    bounded = repo.query(source_name="hh", since=None, until=early)
    assert len(bounded) == 1
    assert bounded[0].timestamp.replace(tzinfo=None) == early.replace(tzinfo=None)


def test_query_orders_newest_first(repo: SqlSourceMetricRepository) -> None:
    """``query`` must return rows sorted by ``timestamp`` desc."""
    base = datetime(2026, 6, 1, tzinfo=UTC)
    repo.record(_make(timestamp=base))
    repo.record(_make(timestamp=base + timedelta(minutes=5)))
    repo.record(_make(timestamp=base + timedelta(minutes=10)))

    events = repo.query(source_name="hh", since=None, until=None)
    # sqlite drops tzinfo on round-trip; compare naive values.
    naive = [e.timestamp.replace(tzinfo=None) for e in events]
    assert naive == [
        (base + timedelta(minutes=10)).replace(tzinfo=None),
        (base + timedelta(minutes=5)).replace(tzinfo=None),
        base.replace(tzinfo=None),
    ]
