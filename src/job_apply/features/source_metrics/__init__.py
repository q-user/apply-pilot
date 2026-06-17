"""Source-metrics slice (M7, issue #62).

This vertical slice records per-source ingest metrics so the operator
can see, per source, how many vacancies were fetched, normalised,
deduplicated, and how many failed — along with the wall-clock
duration of every ingest call. The data model is the source of
truth; an exporter (Prometheus, OpenTelemetry, ...) can be layered
on top later without changing the public contract.

Public surface
--------------

* :class:`SourceMetricEventKind` — the four kinds the issue spec
  pins: :attr:`FETCH`, :attr:`NORMALIZE`, :attr:`DEDUPE`, :attr:`FAIL`.
* :class:`SourceMetricEvent` — the immutable, in-memory value object
  the service hands to the repository.
* :class:`SourceMetricEventORM` — the SQLAlchemy ORM row that backs
  :class:`SourceMetricEvent` in production.
* :class:`SourceMetricRepository` — the Protocol the service depends on.
* :class:`InMemorySourceMetricRepository` — list-backed fake for tests.
* :class:`SqlSourceMetricRepository` — SQLAlchemy-backed production impl.
* :class:`SourceMetricsService` — the high-level facade
  :class:`SourceService` calls per ingest.
* :class:`SourceMetricRead` — the Pydantic DTO the admin API returns.
* :func:`get_source_metric_repository` — FastAPI dependency factory
  used by ``GET /admin/sources/metrics``.
* :data:`router` — FastAPI router (mounted at ``/admin``).
"""

from __future__ import annotations

from job_apply.features.source_metrics.models import (
    SourceMetricEvent as SourceMetricEvent,
)
from job_apply.features.source_metrics.models import (
    SourceMetricEventKind as SourceMetricEventKind,
)
from job_apply.features.source_metrics.models import (
    SourceMetricEventORM as SourceMetricEventORM,
)
from job_apply.features.source_metrics.repository import (
    InMemorySourceMetricRepository as InMemorySourceMetricRepository,
)
from job_apply.features.source_metrics.repository import (
    SourceMetricRepository as SourceMetricRepository,
)
from job_apply.features.source_metrics.repository import (
    SqlSourceMetricRepository as SqlSourceMetricRepository,
)
from job_apply.features.source_metrics.schemas import (
    SourceMetricRead as SourceMetricRead,
)
from job_apply.features.source_metrics.schemas import (
    source_metric_event_to_read as source_metric_event_to_read,
)
from job_apply.features.source_metrics.service import (
    SourceMetricsService as SourceMetricsService,
)

__all__ = [
    "InMemorySourceMetricRepository",
    "SourceMetricEvent",
    "SourceMetricEventKind",
    "SourceMetricEventORM",
    "SourceMetricRead",
    "SourceMetricRepository",
    "SourceMetricsService",
    "SqlSourceMetricRepository",
    "source_metric_event_to_read",
]
