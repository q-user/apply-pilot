"""Tests for the metrics wiring inside :class:`SourceService`.

The metrics slice is wired at the :class:`SourceService` boundary
rather than the :class:`SourceAdapter` boundary. Every public ingest
method (:meth:`SourceService.ingest_vacancy_deduped` and
:meth:`SourceService.ingest_batch`) records one
:class:`SourceMetricEvent` per kind, with the counts and the
wall-clock duration observed during the call.

The metrics service is constructor-injected; ``None`` disables
recording so existing call-sites that do not need metrics
(``ingest_vacancy`` for raw payloads, used by the screening-extractor
tests) stay metric-free.
"""

from __future__ import annotations

import uuid

import pytest

from apply_pilot.features.source_metrics.models import (
    SourceMetricEventKind,
)
from apply_pilot.features.source_metrics.repository import (
    InMemorySourceMetricRepository,
)
from apply_pilot.features.source_metrics.service import SourceMetricsService
from apply_pilot.features.sources.models import Vacancy
from apply_pilot.features.sources.repository import InMemoryVacancyRepository
from apply_pilot.features.sources.service import SourceService


@pytest.fixture
def vacancy_repo() -> InMemoryVacancyRepository:
    """Fresh in-memory vacancy repository per test."""
    return InMemoryVacancyRepository()


@pytest.fixture
def metric_repo() -> InMemorySourceMetricRepository:
    """Fresh in-memory metrics repository per test."""
    return InMemorySourceMetricRepository()


@pytest.fixture
def service(
    vacancy_repo: InMemoryVacancyRepository,
    metric_repo: InMemorySourceMetricRepository,
) -> SourceService:
    """SourceService wired with the metrics service."""
    metrics = SourceMetricsService(metric_repo=metric_repo)
    return SourceService(vacancy_repo, metrics=metrics)


def _vacancy(
    *, source: str = "hh", source_id: str | None = None, content_hash: str | None = "hash-1"
) -> Vacancy:
    """Build a fully-formed :class:`Vacancy` suitable for ``ingest_batch``."""
    return Vacancy(
        id=uuid.uuid4(),
        source=source,
        source_id=source_id or uuid.uuid4().hex,
        title="Test",
        description=None,
        url=None,
        salary_from=None,
        salary_to=None,
        salary_currency="RUR",
        salary_gross=False,
        employer_name=None,
        location=None,
        schedule=None,
        experience=None,
        skills=None,
        published_at=None,
        source_updated_at=None,
        raw_data={"id": "1"},
        content_hash=content_hash,
    )


@pytest.mark.asyncio
async def test_ingest_batch_records_four_kinds(
    service: SourceService, metric_repo: InMemorySourceMetricRepository
) -> None:
    """A single ``ingest_batch`` call records one event per kind."""
    batch = [
        _vacancy(source="hh", source_id="v-001", content_hash="a"),
        _vacancy(source="hh", source_id="v-002", content_hash="b"),
    ]
    # One duplicate by source_id; one duplicate by content_hash.
    batch.append(_vacancy(source="hh", source_id="v-001", content_hash="c"))
    batch.append(_vacancy(source="hh", source_id="v-003", content_hash="a"))  # hash dup

    new, dups = await service.ingest_batch(batch)
    # Sanity-check the dedup itself.
    assert len(new) == 2
    assert len(dups) == 2

    events = metric_repo.query(source_name="hh", since=None, until=None)
    assert len(events) == 4
    by_kind = {e.kind: e for e in events}
    # Fetched == batch size; the rest are dedup-relative.
    assert by_kind[SourceMetricEventKind.FETCH].count == 4
    assert by_kind[SourceMetricEventKind.NORMALIZE].count == 4
    assert by_kind[SourceMetricEventKind.DEDUPE].count == 2
    assert by_kind[SourceMetricEventKind.FAIL].count == 0


@pytest.mark.asyncio
async def test_ingest_batch_records_duration(
    service: SourceService, metric_repo: InMemorySourceMetricRepository
) -> None:
    """The recorded ``duration_ms`` is non-negative for a real call."""
    batch = [_vacancy(source="hh", content_hash=str(uuid.uuid4()))]
    await service.ingest_batch(batch)

    events = metric_repo.query(source_name="hh", since=None, until=None)
    assert len(events) == 4
    for e in events:
        assert e.duration_ms >= 0


@pytest.mark.asyncio
async def test_ingest_batch_records_shared_timestamp(
    service: SourceService, metric_repo: InMemorySourceMetricRepository
) -> None:
    """All four events for one batch share the same ``timestamp``."""
    batch = [_vacancy(source="hh", content_hash=str(uuid.uuid4()))]
    await service.ingest_batch(batch)

    events = metric_repo.query(source_name="hh", since=None, until=None)
    timestamps = {e.timestamp for e in events}
    assert len(timestamps) == 1


@pytest.mark.asyncio
async def test_ingest_vacancy_deduped_records_metrics(
    service: SourceService, metric_repo: InMemorySourceMetricRepository
) -> None:
    """``ingest_vacancy_deduped`` records one set of four events per call."""
    payload = {
        "id": "v-100",
        "name": "New Role",
        "description": "Test",
        "employer": {"name": "Acme"},
    }

    await service.ingest_vacancy_deduped("hh", payload)

    events = metric_repo.query(source_name="hh", since=None, until=None)
    assert len(events) == 4
    by_kind = {e.kind: e for e in events}
    assert by_kind[SourceMetricEventKind.FETCH].count == 1
    assert by_kind[SourceMetricEventKind.NORMALIZE].count == 1
    assert by_kind[SourceMetricEventKind.DEDUPE].count == 0
    assert by_kind[SourceMetricEventKind.FAIL].count == 0


@pytest.mark.asyncio
async def test_ingest_vacancy_deduped_duplicate_records_dedup_count(
    service: SourceService, metric_repo: InMemorySourceMetricRepository
) -> None:
    """A duplicate via ``ingest_vacancy_deduped`` records a non-zero dedup count."""
    payload = {
        "id": "v-200",
        "name": "Role",
        "description": "Test",
        "employer": {"name": "Acme"},
    }
    await service.ingest_vacancy_deduped("hh", payload)
    result = await service.ingest_vacancy_deduped("hh", payload)
    assert result is None  # duplicate

    events = metric_repo.query(source_name="hh", since=None, until=None)
    dedup_events = [e for e in events if e.kind == SourceMetricEventKind.DEDUPE]
    # The 1st call records a zero-count dedup event; the 2nd records a
    # one-count dedup event. At least one of them has count=1.
    assert any(e.count == 1 for e in dedup_events)
    assert any(e.count == 0 for e in dedup_events)


@pytest.mark.asyncio
async def test_metrics_disabled_when_none(
    vacancy_repo: InMemoryVacancyRepository,
) -> None:
    """A ``SourceService(metrics=None)`` records nothing (no exceptions)."""
    service = SourceService(vacancy_repo, metrics=None)
    await service.ingest_batch([_vacancy(source="hh", content_hash=str(uuid.uuid4()))])
