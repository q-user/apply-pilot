"""sources — ingest and normalise vacancies from external job boards.

This vertical slice owns the canonical :class:`Vacancy` model and the
plumbing that turns raw, source-specific payloads into a single
shape the rest of the application can query against.

Public surface
--------------

* :class:`Vacancy` — the ORM model.
* :class:`VacancyNormalizer` — payload → Vacancy mapper (hh.ru today).
* :class:`VacancyRepository` — the storage Protocol the service depends on.
* :class:`InMemoryVacancyRepository` — test/dict-backed implementation.
* :class:`SqlVacancyRepository` — production SQLAlchemy implementation.
* :class:`VacancyDeduplicator` — content-hash cross-source dedup pre-write.
* :class:`SourceService` — the ingest use case:
  :meth:`SourceService.ingest_vacancy` (raw → upsert),
  :meth:`SourceService.ingest_vacancy_deduped` (raw → dedup → upsert),
  :meth:`SourceService.ingest_batch` (batched dedup → upsert),
  :meth:`SourceService.list_vacancies` (filtered + paginated read).
* :class:`VacancyListResult` — the dataclass returned by
  :meth:`SourceService.list_vacancies`.
* :class:`VacancyRead` / :class:`VacancyListResponse` — public Pydantic
  DTOs for the ``GET /vacancies`` endpoint.
* :class:`SourceAdapter` — the cross-source Protocol (M7, issue #70).
* :class:`SourceQuery` — source-agnostic search filter.
* :class:`AdapterRegistry` — in-memory index of
  :class:`SourceAdapter` instances keyed by :attr:`~SourceAdapter.name`.
* :class:`CircuitBreaker` — per-source circuit-breaker state machine
  (M7, issue #61).
* :class:`CircuitState` — the three breaker states (``closed``,
  ``open``, ``half_open``).
* :class:`BreakerSettings` — the frozen tunables dataclass
  (``failure_threshold``, ``reset_timeout_seconds``,
  ``half_open_max_calls``).
* :class:`SourceCircuitRegistry` — the Protocol for the per-source
  breaker index; :class:`InMemorySourceCircuitRegistry` is the
  default dict-backed implementation.
* :class:`SourceUnavailableError` — the exception a
  :class:`BreakeredSourceAdapter` raises when the breaker is
  :attr:`~CircuitState.OPEN`.
* :class:`BreakeredSourceAdapter` — a
  :class:`SourceAdapter` decorator that consults the breaker
  before forwarding every call.
* :data:`router` — FastAPI router (currently exposes ``GET /vacancies``).
"""

from __future__ import annotations

from job_apply.features.sources.adapter import (
    AdapterRegistry as AdapterRegistry,
)
from job_apply.features.sources.adapter import (
    SourceAdapter as SourceAdapter,
)
from job_apply.features.sources.adapter import (
    SourceQuery as SourceQuery,
)
from job_apply.features.sources.breaker import (
    BreakeredSourceAdapter as BreakeredSourceAdapter,
)
from job_apply.features.sources.breaker import (
    BreakerSettings as BreakerSettings,
)
from job_apply.features.sources.breaker import (
    CircuitBreaker as CircuitBreaker,
)
from job_apply.features.sources.breaker import (
    CircuitState as CircuitState,
)
from job_apply.features.sources.breaker import (
    InMemorySourceCircuitRegistry as InMemorySourceCircuitRegistry,
)
from job_apply.features.sources.breaker import (
    SourceCircuitRegistry as SourceCircuitRegistry,
)
from job_apply.features.sources.breaker import (
    SourceUnavailableError as SourceUnavailableError,
)
from job_apply.features.sources.dedup import VacancyDeduplicator
from job_apply.features.sources.models import Vacancy
from job_apply.features.sources.normalizer import VacancyNormalizer
from job_apply.features.sources.repository import (
    InMemoryVacancyRepository,
    SqlVacancyRepository,
    VacancyRepository,
)
from job_apply.features.sources.schemas import VacancyListResponse, VacancyRead
from job_apply.features.sources.search_service import IngestResult as IngestResult
from job_apply.features.sources.search_service import (
    VacancySearchService as VacancySearchService,
)
from job_apply.features.sources.service import SourceService, VacancyListResult

__all__ = [
    "AdapterRegistry",
    "BreakerSettings",
    "BreakeredSourceAdapter",
    "CircuitBreaker",
    "CircuitState",
    "InMemorySourceCircuitRegistry",
    "InMemoryVacancyRepository",
    "IngestResult",
    "SourceAdapter",
    "SourceCircuitRegistry",
    "SourceQuery",
    "SourceService",
    "SourceUnavailableError",
    "SqlVacancyRepository",
    "Vacancy",
    "VacancyDeduplicator",
    "VacancyListResponse",
    "VacancyListResult",
    "VacancyNormalizer",
    "VacancyRead",
    "VacancyRepository",
    "VacancySearchService",
]
