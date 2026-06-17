"""Source ingestion business logic.

The ``SourceService`` owns the ingest pipeline: normalise raw source data
into a canonical ``Vacancy``, deduplicate it, and persist it via the
repository. As of M2 (issue #26) the service also delegates the
screening-question capture to an optional
:class:`~job_apply.features.screening.extractor.ScreeningQuestionExtractor`
that the caller injects; the sources slice stays agnostic of the
screening schema.

As of M7 (issue #62) the service also records per-source ingest metrics
(via an optional
:class:`~job_apply.features.source_metrics.service.SourceMetricsService`).
The metrics service is constructor-injected; ``None`` disables
recording so existing call-sites that do not need metrics stay
metric-free.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any

from job_apply.features.screening.models import ScreeningQuestion
from job_apply.features.sources.dedup import VacancyDeduplicator
from job_apply.features.sources.models import Vacancy
from job_apply.features.sources.normalizer import VacancyNormalizer
from job_apply.features.sources.repository import VacancyRepository

if TYPE_CHECKING:
    from job_apply.features.screening.extractor import ScreeningQuestionExtractor
    from job_apply.features.source_metrics.service import SourceMetricsService

logger = logging.getLogger(__name__)


class SourceService:
    """Ingest vacancies from external job boards.

    The service composes three collaborators:

    * :class:`VacancyNormalizer` — turns raw source payloads into ``Vacancy``.
    * :class:`VacancyDeduplicator` — short-circuits known rows.
    * :class:`VacancyRepository` — persists what survives dedup.

    A fourth collaborator — a
    :class:`~job_apply.features.screening.extractor.ScreeningQuestionExtractor`
    — is **optional**. When supplied via
    :meth:`ingest_vacancy` the service runs the extractor after the
    vacancy is upserted, persists the resulting screening questions
    and returns them. When not supplied the service still upserts the
    vacancy but the screening-capture step is a no-op.

    A fifth collaborator — a
    :class:`~job_apply.features.source_metrics.service.SourceMetricsService`
    — is also **optional**. When supplied, every public ingest call
    records a set of four metric events (FETCH / NORMALIZE / DEDUPE /
    FAIL) with the same ``timestamp`` and ``duration_ms`` so the
    operator can see what happened during that call. When not
    supplied the service stays metric-free.
    """

    def __init__(
        self,
        repository: VacancyRepository,
        *,
        normalizer: VacancyNormalizer | None = None,
        deduplicator: VacancyDeduplicator | None = None,
        metrics: SourceMetricsService | None = None,
    ) -> None:
        self._repo = repository
        self._normalizer = normalizer or VacancyNormalizer()
        self._deduplicator = deduplicator or VacancyDeduplicator(repository)
        self._metrics = metrics

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

    @property
    def metrics(self) -> SourceMetricsService | None:
        """Expose the metrics service for tests that need to assert state."""
        return self._metrics

    def ingest_vacancy(
        self,
        source: str,
        raw_data: dict[str, Any],
        *,
        screening_extractor: ScreeningQuestionExtractor | None = None,
    ) -> list[ScreeningQuestion]:
        """Normalise raw source data, upsert the vacancy, and capture screening questions.

        The vacancy is always upserted via the repository. If a
        ``screening_extractor`` is provided, it runs *after* the
        upsert (so the vacancy's id is already assigned) and the
        captured questions are returned; otherwise the method returns
        an empty list.

        The Vacancy itself is **not** part of the return value; the
        repository exposes the just-persisted row via
        :meth:`VacancyRepository.find_by_source` /
        :meth:`VacancyRepository.get_by_id`, which is what the rest
        of the application uses for further lookups.
        """
        vacancy = self._normalizer.normalize(source, raw_data)
        self._repo.upsert(vacancy)
        if screening_extractor is None:
            return []
        return screening_extractor.extract_from_vacancy(vacancy, raw_data)

    async def ingest_vacancy_deduped(self, source: str, raw_data: dict) -> Vacancy | None:
        """Normalise, dedup, and (only if new) persist a single vacancy.

        Returns the persisted :class:`Vacancy` or ``None`` when the
        incoming row is detected as a duplicate.

        When a :class:`SourceMetricsService` was injected, the call
        records one set of four metric events with
        ``fetched=normalized=1`` and ``deduped=0`` or ``1`` depending
        on whether the row was a duplicate.
        """
        metrics = self._metrics
        started = time.monotonic() if metrics is not None else None
        try:
            vacancy = self._normalizer.normalize(source, raw_data)
            if await self._deduplicator.is_duplicate(vacancy):
                logger.info(
                    "Skipping duplicate vacancy source=%s source_id=%s",
                    vacancy.source,
                    vacancy.source_id,
                )
                self._record_metrics(
                    source=vacancy.source,
                    fetched=1,
                    normalized=1,
                    deduped=1,
                    failed=0,
                    started=started,
                )
                return None
            persisted = self._repo.upsert(vacancy)
            self._record_metrics(
                source=persisted.source,
                fetched=1,
                normalized=1,
                deduped=0,
                failed=0,
                started=started,
            )
            return persisted
        except Exception:
            self._record_metrics(
                source=source,
                fetched=1,
                normalized=0,
                deduped=0,
                failed=1,
                started=started,
            )
            raise

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
        metrics = self._metrics
        started = time.monotonic() if metrics is not None else None
        source_name = vacancies[0].source if vacancies else "unknown"
        try:
            new, duplicates = await self._deduplicator.deduplicate_batch(vacancies)
            for vacancy in new:
                self._repo.upsert(vacancy)
            logger.info(
                "Ingest batch processed: persisted=%d skipped=%d",
                len(new),
                len(duplicates),
            )
            self._record_metrics(
                source=source_name,
                fetched=len(vacancies),
                normalized=len(vacancies),
                deduped=len(duplicates),
                failed=0,
                started=started,
            )
            return new, duplicates
        except Exception:
            self._record_metrics(
                source=source_name,
                fetched=len(vacancies),
                normalized=len(vacancies),
                deduped=0,
                failed=1,
                started=started,
            )
            raise

    def _record_metrics(
        self,
        *,
        source: str,
        fetched: int,
        normalized: int,
        deduped: int,
        failed: int,
        started: float | None,
    ) -> None:
        """Record one set of metric events for an ingest call.

        No-op when ``metrics`` is ``None`` or when ``started`` is
        ``None`` (the caller did not start a monotonic clock because
        no metrics service was injected).
        """
        metrics = self._metrics
        if metrics is None or started is None:
            return
        duration_ms = max(0, int((time.monotonic() - started) * 1000))
        metrics.record_ingest(
            source_name=source,
            fetched=fetched,
            normalized=normalized,
            deduped=deduped,
            failed=failed,
            duration_ms=duration_ms,
        )

    def list_vacancies(
        self,
        *,
        source: str | None = None,
        salary_min: int | None = None,
        location: str | None = None,
        since: datetime | None = None,
        limit: int = 20,
        offset: int = 0,
    ) -> VacancyListResult:
        """Return a paginated, filtered slice of vacancies.

        Thin orchestration over the repository: a ``COUNT(*)`` for the
        total and a ``SELECT`` for the page. The API layer maps
        :class:`VacancyListResult` onto :class:`VacancyListResponse`.

        All filter arguments are optional; ``None`` means "do not
        filter on this dimension". The repository is responsible for
        applying the predicates consistently in both the count and the
        list call (they share a single filter-builder so the two cannot
        drift apart).
        """
        total = self._repo.count_with_filters(
            source=source,
            salary_min=salary_min,
            location=location,
            since=since,
        )
        items = list(
            self._repo.list_with_filters(
                source=source,
                salary_min=salary_min,
                location=location,
                since=since,
                limit=limit,
                offset=offset,
            )
        )
        return VacancyListResult(items=items, total=total)


@dataclass(frozen=True, slots=True)
class VacancyListResult:
    """The outcome of :meth:`SourceService.list_vacancies`.

    ``items`` is the current page (already ordered by ``created_at``
    desc by the repository), ``total`` is the total row count that
    matched the filter set, regardless of pagination. The API layer
    wraps this in :class:`VacancyListResponse`.
    """

    items: list[Vacancy]
    total: int


__all__ = ["SourceService", "VacancyListResult"]
