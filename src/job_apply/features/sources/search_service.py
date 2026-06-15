"""Cross-source vacancy search + ingest service.

The :class:`VacancySearchService` is the use-case that ties a search
client (e.g. :class:`~job_apply.features.hh.search.HHVacancySearchClient`)
to the canonical ingest pipeline
(:class:`~job_apply.features.sources.service.SourceService`). It is the
single entry point the application code uses to "fetch a batch of
vacancies from a source and persist what is new".

The service lives in the sources slice (not the hh slice) so it can be
reused for any future source that exposes a
:class:`~job_apply.features.hh.search.HHVacancySearchClient`-shaped
client. Source-specific concerns (HTTP transport, error mapping,
authentication) stay in the source's own slice.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass

from job_apply.features.sources.service import SourceService

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class IngestResult:
    """The outcome of a single :meth:`VacancySearchService.search_and_ingest` call.

    The three counts let callers reason about *why* a fetch produced the
    rows it did:

    * ``fetched`` — raw vacancies returned by the search client.
    * ``persisted`` — vacancies that were new and written to the
      repository.
    * ``skipped`` — vacancies that matched an existing row (or another
      vacancy in the same batch) by ``(source, source_id)`` or
      ``content_hash`` and were therefore not written.

    Invariant: ``persisted + skipped == fetched``.
    """

    fetched: int
    persisted: int
    skipped: int


class VacancySearchService:
    """Orchestrate a source search and persist the new rows.

    The service is intentionally thin: it is a *composition* of the
    search client and the canonical :class:`SourceService`, not a
    re-implementation of either. This keeps dedup and persistence
    behaviour in one place and avoids drift between the search path and
    the raw-payload ingest path.
    """

    def __init__(
        self,
        *,
        client: object,
        source_service: SourceService,
    ) -> None:
        # ``client`` is typed as ``object`` because the protocol is
        # declared on the source slice (HH). Importing the protocol
        # here would invert the dependency direction (sources → hh).
        # The structural type is checked at call time via ``search`` /
        # ``fetch_one`` being async callables; tests use a duck-typed
        # fake to keep the seam clear.
        self._client = client
        self._source_service = source_service

    async def search_and_ingest(
        self,
        user_id: uuid.UUID,
        query: object,
    ) -> IngestResult:
        """Fetch a batch from the source and persist what is new.

        ``user_id`` is reserved for source-specific credential lookup
        (e.g. attaching a Bearer token for hh.ru). The public search
        endpoint does not require auth, so the in-memory client and the
        default HTTP client ignore it. A future production wiring will
        pass it through to the client's credential-bound overload.

        The flow is:

        1. ``raw = await client.search(query)`` — list of dicts.
        2. ``vacancies = [normalizer.normalize_hh(r) for r in raw]``.
        3. ``new, dups = await source_service.ingest_batch(vacancies)``.
        4. Return an :class:`IngestResult` summarising the counts.
        """
        raw = await self._client.search(query)
        normalizer = self._source_service.normalizer
        vacancies = [normalizer.normalize_hh(item) for item in raw]
        new, duplicates = await self._source_service.ingest_batch(vacancies)

        result = IngestResult(
            fetched=len(raw),
            persisted=len(new),
            skipped=len(duplicates),
        )
        logger.info(
            "VacancySearchService.search_and_ingest user_id=%s fetched=%d persisted=%d skipped=%d",
            user_id,
            result.fetched,
            result.persisted,
            result.skipped,
        )
        return result


__all__ = ["IngestResult", "VacancySearchService"]
