"""TDD tests for :class:`SourceMetricsService`.

The service is the high-level facade :class:`SourceService` calls
once per ingest invocation. ``record_ingest`` writes a single
:class:`SourceMetricEvent` per kind (FETCH / NORMALIZE / DEDUPE /
FAIL) with the same ``duration_ms`` and a timestamp from a
monotonically-advancing clock so callers can correlate batches.

A ``clock`` callable is injected so the tests can pin timestamps
without monkey-patching the module.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest

from job_apply.features.source_metrics.models import (
    SourceMetricEventKind,
)
from job_apply.features.source_metrics.repository import (
    InMemorySourceMetricRepository,
)
from job_apply.features.source_metrics.service import SourceMetricsService


@pytest.fixture
def repo() -> Iterator[InMemorySourceMetricRepository]:
    """Fresh in-memory repository per test."""
    yield InMemorySourceMetricRepository()


@pytest.fixture
def clock() -> Iterator[list[datetime]]:
    """A mutable clock the tests can advance.

    The service receives a ``() -> datetime`` callable; mutating the
    list element between calls is cheaper than monkey-patching.
    """
    base = datetime(2026, 6, 17, 12, 0, 0, tzinfo=UTC)
    now: list[datetime] = [base]
    yield now


@pytest.fixture
def service(
    repo: InMemorySourceMetricRepository,
    clock: list[datetime],
) -> SourceMetricsService:
    """Service wired to the in-memory repo and the test clock."""
    return SourceMetricsService(metric_repo=repo, clock=lambda: clock[0])


def test_record_ingest_persists_four_kinds(
    service: SourceMetricsService, repo: InMemorySourceMetricRepository
) -> None:
    """``record_ingest`` must write one event per kind (fetch, normalize, dedupe, fail)."""
    service.record_ingest(
        source_name="hh",
        fetched=10,
        normalized=9,
        deduped=3,
        failed=1,
        duration_ms=250,
    )

    events = repo.query(source_name="hh", since=None, until=None)
    assert len(events) == 4
    by_kind = {e.kind: e for e in events}
    assert by_kind[SourceMetricEventKind.FETCH].count == 10
    assert by_kind[SourceMetricEventKind.NORMALIZE].count == 9
    assert by_kind[SourceMetricEventKind.DEDUPE].count == 3
    assert by_kind[SourceMetricEventKind.FAIL].count == 1

    # All four events share the same wall-clock duration_ms.
    assert {e.duration_ms for e in events} == {250}


def test_record_ingest_uses_injected_clock(
    service: SourceMetricsService, repo: InMemorySourceMetricRepository, clock: list[datetime]
) -> None:
    """All four events share the same ``timestamp`` from the injected clock."""
    service.record_ingest(
        source_name="hh",
        fetched=1,
        normalized=1,
        deduped=0,
        failed=0,
        duration_ms=10,
    )
    events = repo.query(source_name="hh", since=None, until=None)
    assert {e.timestamp for e in events} == {clock[0]}


def test_record_ingest_zero_counts_still_recorded(
    service: SourceMetricsService, repo: InMemorySourceMetricRepository
) -> None:
    """A zero count must still produce an event — the absence of data is a data point."""
    service.record_ingest(
        source_name="hh",
        fetched=0,
        normalized=0,
        deduped=0,
        failed=0,
        duration_ms=10,
    )
    events = repo.query(source_name="hh", since=None, until=None)
    assert len(events) == 4
    for e in events:
        assert e.count == 0


def test_record_ingest_metadata_carries_counts(
    service: SourceMetricsService, repo: InMemorySourceMetricRepository
) -> None:
    """The ``metadata`` field mirrors the counts so ad-hoc SQL queries can GROUP BY it."""
    service.record_ingest(
        source_name="hh",
        fetched=10,
        normalized=9,
        deduped=3,
        failed=1,
        duration_ms=250,
        metadata={"vacancy_id": "v-001"},
    )

    events = repo.query(source_name="hh", since=None, until=None)
    for e in events:
        assert e.metadata["fetched"] == 10
        assert e.metadata["normalized"] == 9
        assert e.metadata["deduped"] == 3
        assert e.metadata["failed"] == 1
        assert e.metadata["duration_ms"] == 250
        assert e.metadata["vacancy_id"] == "v-001"


def test_record_ingest_multiple_calls_produce_distinct_events(
    service: SourceMetricsService, repo: InMemorySourceMetricRepository, clock: list[datetime]
) -> None:
    """Two consecutive ``record_ingest`` calls produce two sets of four events each."""
    service.record_ingest(
        source_name="hh",
        fetched=5,
        normalized=5,
        deduped=1,
        failed=0,
        duration_ms=100,
    )
    clock[0] += timedelta(minutes=5)
    service.record_ingest(
        source_name="hh",
        fetched=3,
        normalized=2,
        deduped=0,
        failed=1,
        duration_ms=50,
    )

    events = repo.query(source_name="hh", since=None, until=None)
    assert len(events) == 8
    timestamps = {e.timestamp for e in events}
    assert len(timestamps) == 2  # two distinct timestamps


def test_record_ingest_different_sources_isolated(
    service: SourceMetricsService, repo: InMemorySourceMetricRepository
) -> None:
    """``record_ingest`` keeps each source's events under its own name."""
    service.record_ingest(
        source_name="hh", fetched=1, normalized=1, deduped=0, failed=0, duration_ms=10
    )
    service.record_ingest(
        source_name="habr", fetched=2, normalized=2, deduped=0, failed=0, duration_ms=20
    )

    hh = repo.query(source_name="hh", since=None, until=None)
    habr = repo.query(source_name="habr", since=None, until=None)
    assert len(hh) == 4
    assert len(habr) == 4
