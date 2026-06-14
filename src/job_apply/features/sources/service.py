"""Source ingestion business logic.

The ``SourceService`` owns the ingest pipeline: normalise raw source data
into a canonical ``Vacancy``, deduplicate it, and persist it via the
repository.
"""

from __future__ import annotations

import logging

from job_apply.features.sources.dedup import VacancyDeduplicator
from job_apply.features.sources.models import Vacancy
from job_apply.features.sources.normalizer import VacancyNormalizer
from job_apply.features.sources.repository import VacancyRepository

logger = logging.getLogger(__name__)


class SourceService:
    """Ingest vacancies from external job boards.

    The service composes three collaborators:

    * :class:`VacancyNormalizer` — turns raw source payloads into ``Vacancy``.
    * :class:`VacancyDeduplicator` — short-circuits known rows.
    * :class:`VacancyRepository` — persists what survives dedup.
    """

    def __init__(
        self,
        repository: VacancyRepository,
        *,
        normalizer: VacancyNormalizer | None = None,
        deduplicator: VacancyDeduplicator | None = None,
    ) -> None:
        self._repo = repository
        self._normalizer = normalizer or VacancyNormalizer()
        self._deduplicator = deduplicator or VacancyDeduplicator(repository)

    @property
    def repo(self) -> VacancyRepository:
        """Expose the repository for tests that need to assert state."""
        return self._repo

    @property
    def normalizer(self) -> VacancyNormalizer:
        """Expose the normaliser for tests that inspect the produced Vacancy."""
        return self._normalizer

    @property
    def deduplicator(self) -> VacancyDeduplicator:
        """Expose the deduplicator for tests that need to assert state."""
        return self._deduplicator

    def ingest_vacancy(self, source: str, raw_data: dict) -> Vacancy:
        """Normalise raw source data and upsert into the repository.

        Returns the persisted ``Vacancy``.
        """
        vacancy = self._normalizer.normalize(source, raw_data)
        return self._repo.upsert(vacancy)

    async def ingest_vacancy_deduped(self, source: str, raw_data: dict) -> Vacancy | None:
        """Normalise, dedup, and (only if new) persist a single vacancy.

        Returns the persisted :class:`Vacancy` or ``None`` when the
        incoming row is detected as a duplicate.
        """
        vacancy = self._normalizer.normalize(source, raw_data)
        if await self._deduplicator.is_duplicate(vacancy):
            logger.info(
                "Skipping duplicate vacancy source=%s source_id=%s",
                vacancy.source,
                vacancy.source_id,
            )
            return None
        return self._repo.upsert(vacancy)

    async def ingest_batch(self, vacancies: list[Vacancy]) -> tuple[list[Vacancy], list[Vacancy]]:
        """Deduplicate then persist a batch of vacancies.

        ``vacancies`` are pre-normalised :class:`Vacancy` instances. The
        caller's normaliser is responsible for the source-specific mapping;
        this method only handles the dedup + persist pipeline.

        Returns ``(new, duplicates)``:

        * ``new`` — vacancies persisted via :meth:`VacancyRepository.upsert`.
        * ``duplicates`` — vacancies that matched an existing row or
          another item in the same batch by ``(source, source_id)`` or
          ``content_hash``.

        The number of skipped duplicates is logged at ``info`` level.
        """
        new, duplicates = await self._deduplicator.deduplicate_batch(vacancies)
        for vacancy in new:
            self._repo.upsert(vacancy)
        logger.info(
            "Ingest batch processed: persisted=%d skipped=%d",
            len(new),
            len(duplicates),
        )
        return new, duplicates


__all__ = ["SourceService"]
