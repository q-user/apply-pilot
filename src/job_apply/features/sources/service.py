"""Source-ingestion business logic.

The :class:`SourceService` owns the ingest pipeline: take a raw source
payload, normalise it through :class:`VacancyNormalizer`, and persist it
through the :class:`VacancyRepository` (which performs the upsert).

The service is intentionally thin — it composes two collaborators and
exposes the result. Cross-cutting concerns (audit, deduplication beyond
the natural key, scheduled refresh) will be layered on top in follow-up
issues.
"""

from __future__ import annotations

from typing import Any

from job_apply.features.sources.models import Vacancy
from job_apply.features.sources.normalizer import VacancyNormalizer
from job_apply.features.sources.repository import VacancyRepository


class SourceService:
    """Ingest vacancies from external job boards."""

    def __init__(
        self,
        repository: VacancyRepository,
        *,
        normalizer: VacancyNormalizer | None = None,
    ) -> None:
        self._repo = repository
        self._normalizer = normalizer or VacancyNormalizer()

    @property
    def repo(self) -> VacancyRepository:
        """Expose the repository for tests that need to assert state."""
        return self._repo

    @property
    def normalizer(self) -> VacancyNormalizer:
        """Expose the normaliser for tests that inspect the produced Vacancy."""
        return self._normalizer

    def ingest_vacancy(self, source: str, raw_data: dict[str, Any]) -> Vacancy:
        """Normalise ``raw_data`` and upsert it into the repository.

        Returns the persisted :class:`Vacancy` (existing rows are updated
        in place; the returned object's ``id`` matches the existing row's
        ``id``).
        """
        vacancy = self._normalizer.normalize(source, raw_data)
        return self._repo.upsert(vacancy)


__all__ = ["SourceService"]
