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
* :data:`router` — FastAPI router (currently exposes ``GET /vacancies``).
"""

from __future__ import annotations

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
    "InMemoryVacancyRepository",
    "IngestResult",
    "SourceService",
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
