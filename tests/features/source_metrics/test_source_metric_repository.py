"""TDD tests for :class:`InMemorySourceMetricRepository`.

The :class:`SourceMetricRepository` Protocol is the persistence
contract. This module covers the in-memory implementation — the
SQL-backed implementation is covered separately in
``test_source_metric_repository_sql.py``.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest

from apply_pilot.features.source_metrics.models import (
    SourceMetricEvent,
    SourceMetricEventKind,
)
from apply_pilot.features.source_metrics.repository import (
    InMemorySourceMetricRepository,
)


@pytest.fixture
def repo() -> Iterator[InMemorySourceMetricRepository]:
    """Fresh in-memory repository per test."""
    yield InMemorySourceMetricRepository()


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
        source_name=source,
        kind=kind,
        count=count,
        duration_ms=duration_ms,
        timestamp=timestamp or datetime.now(UTC),
        metadata=metadata or {},
    )


def test_record_persists_event(repo: InMemorySourceMetricRepository) -> None:
    """``record`` must return the event and make it visible via ``query``."""
    event = _make(source="hh", count=5, duration_ms=120)
    saved = repo.record(event)

    assert saved is event
    queried = repo.query(source_name="hh", since=None, until=None)
    assert len(queried) == 1
    assert queried[0].id == event.id


def test_query_filters_by_source_name(repo: InMemorySourceMetricRepository) -> None:
    """``query`` must restrict to the requested source only."""
    repo.record(_make(source="hh"))
    repo.record(_make(source="habr"))
    repo.record(_make(source="hh"))

    hh_events = repo.query(source_name="hh", since=None, until=None)
    assert len(hh_events) == 2
    assert {e.source_name for e in hh_events} == {"hh"}

    habr_events = repo.query(source_name="habr", since=None, until=None)
    assert len(habr_events) == 1
    assert habr_events[0].source_name == "habr"


def test_query_filters_by_since(repo: InMemorySourceMetricRepository) -> None:
    """``query`` must include only events with ``timestamp > since`` (strictly after)."""
    old = datetime(2026, 1, 1, tzinfo=UTC)
    recent = datetime(2026, 6, 1, tzinfo=UTC)
    cutoff = datetime(2026, 3, 1, tzinfo=UTC)

    repo.record(_make(timestamp=old))
    repo.record(_make(timestamp=recent))

    since_events = repo.query(source_name="hh", since=cutoff, until=None)
    # Only the "recent" event satisfies timestamp > cutoff.
    assert len(since_events) == 1
    assert since_events[0].timestamp == recent

    # A ``since`` exactly equal to an event's timestamp is excluded
    # (strict greater-than).
    boundary_events = repo.query(source_name="hh", since=recent, until=None)
    assert len(boundary_events) == 0


def test_query_filters_by_until(repo: InMemorySourceMetricRepository) -> None:
    """``query`` must include only events with ``timestamp <= until`` (inclusive)."""
    early = datetime(2026, 1, 1, tzinfo=UTC)
    late = datetime(2026, 6, 1, tzinfo=UTC)

    repo.record(_make(timestamp=early))
    repo.record(_make(timestamp=late))

    # ``until`` is inclusive — the boundary event is included.
    until_events = repo.query(source_name="hh", since=None, until=early)
    assert len(until_events) == 1
    assert until_events[0].timestamp == early

    # Anything between ``since`` (strict) and ``until`` (inclusive).
    between = repo.query(
        source_name="hh",
        since=datetime(2025, 12, 1, tzinfo=UTC),
        until=early,
    )
    assert len(between) == 1


def test_query_returns_empty_when_no_match(repo: InMemorySourceMetricRepository) -> None:
    """``query`` must return an empty list when nothing matches."""
    repo.record(_make(source="hh"))
    events = repo.query(source_name="habr", since=None, until=None)
    assert events == []


def test_query_orders_newest_first(repo: InMemorySourceMetricRepository) -> None:
    """``query`` must return events sorted by ``timestamp`` desc."""
    base = datetime(2026, 6, 1, tzinfo=UTC)
    repo.record(_make(timestamp=base))
    repo.record(_make(timestamp=base + timedelta(minutes=5)))
    repo.record(_make(timestamp=base + timedelta(minutes=10)))

    events = repo.query(source_name="hh", since=None, until=None)
    assert [e.timestamp for e in events] == [
        base + timedelta(minutes=10),
        base + timedelta(minutes=5),
        base,
    ]


def test_query_window_combinator(repo: InMemorySourceMetricRepository) -> None:
    """Both ``since`` and ``until`` apply together (and combine with the source filter)."""
    base = datetime(2026, 6, 1, tzinfo=UTC)
    repo.record(_make(source="hh", timestamp=base - timedelta(days=2)))
    repo.record(_make(source="hh", timestamp=base - timedelta(hours=12)))
    repo.record(_make(source="hh", timestamp=base))
    repo.record(_make(source="habr", timestamp=base))

    events = repo.query(
        source_name="hh",
        since=base - timedelta(hours=1),  # strictly after
        until=base,
    )
    assert len(events) == 1
    assert events[0].timestamp == base
