"""hh.ru :class:`SourceAdapter` (M7, issue #70).

:class:`HhSourceAdapter` is the hh.ru implementation of the
cross-source :class:`~apply_pilot.features.sources.adapter.SourceAdapter`
Protocol. It is a thin wrapper that composes the four narrow contracts
the slice already ships:

* :class:`~apply_pilot.features.hh.search.HHVacancySearchClient` —
  the search transport (production or in-memory).
* :class:`~apply_pilot.features.sources.normalizer.VacancyNormalizer` —
  turns hh payloads into a canonical
  :class:`~apply_pilot.features.sources.models.Vacancy` (delegates to
  :meth:`VacancyNormalizer.normalize_hh`).
* :class:`~apply_pilot.features.screening.extractor.HhScreeningQuestionExtractor` —
  builds screening question rows from ``raw["questions"]``.
* An :class:`~apply_pilot.features.apply_worker.runtime.ApplyAdapter`
  with ``name == "hh"`` — the apply submission transport (the
  production
  :class:`~apply_pilot.features.hh.apply.HhApplyAdapter` or a fake in
  tests).

The adapter is the single place that wires these collaborators
together. It does not replace any of them — every existing test keeps
passing because the underlying clients remain independently usable.
The cross-source orchestration code (the future
``VacancySearchService`` migration, the :class:`ApplyWorker`) looks
adapters up by :attr:`name` in an
:class:`~apply_pilot.features.sources.adapter.AdapterRegistry` instead
of importing the right collaborator from each feature package.
"""

from __future__ import annotations

from typing import Any

from apply_pilot.features.apply_worker.models import ApplyJob
from apply_pilot.features.apply_worker.runtime import ApplyResult
from apply_pilot.features.screening.extractor import (
    HhScreeningQuestionExtractor,
    ScreeningQuestionExtractor,
)
from apply_pilot.features.screening.models import ScreeningQuestion
from apply_pilot.features.sources.adapter import SourceQuery
from apply_pilot.features.sources.models import Vacancy
from apply_pilot.features.sources.normalizer import VacancyNormalizer


class HhSourceAdapter:
    """hh.ru :class:`SourceAdapter` implementation.

    Translating the cross-source :class:`SourceQuery` into the
    hh-flavored
    :class:`~apply_pilot.features.hh.search.HHQuery` is the adapter's
    only piece of glue logic: the two query types share the same first
    three fields (``text``/``area``/``salary``) and the same
    pagination semantics. :attr:`SourceQuery.extra` is intentionally
    *not* forwarded — the typed ``HHQuery`` is the contract, and
    per-source extras are expected to grow a typed field on the hh
    query in a follow-up issue if needed.
    """

    #: Stable source identifier. Matches
    #: :attr:`~apply_pilot.features.sources.models.Vacancy.source` for
    #: hh-ingested rows and the key the
    #: :class:`~apply_pilot.features.sources.adapter.AdapterRegistry`
    #: looks the adapter up under.
    name: str = "hh"

    def __init__(
        self,
        *,
        search_client: Any,
        normalizer: VacancyNormalizer,
        screening_extractor: HhScreeningQuestionExtractor | ScreeningQuestionExtractor,
        apply_adapter: Any,
    ) -> None:
        self._search_client = search_client
        self._normalizer = normalizer
        self._screening_extractor = screening_extractor
        self._apply_adapter = apply_adapter

    @property
    def search_client(self) -> Any:
        """Return the injected hh search client (read-only)."""
        return self._search_client

    @property
    def normalizer(self) -> VacancyNormalizer:
        """Return the injected vacancy normaliser (read-only)."""
        return self._normalizer

    @property
    def screening_extractor(self) -> HhScreeningQuestionExtractor | ScreeningQuestionExtractor:
        """Return the injected screening extractor (read-only)."""
        return self._screening_extractor

    @property
    def apply_adapter(self) -> Any:
        """Return the injected apply adapter (read-only)."""
        return self._apply_adapter

    # ------------------------------------------------------------------
    # SourceAdapter
    # ------------------------------------------------------------------

    async def search(self, query: SourceQuery) -> list[dict[str, Any]]:
        """Translate ``query`` into an :class:`HHQuery` and call the search client.

        :attr:`SourceQuery.extra` is dropped on purpose — the typed
        :class:`~apply_pilot.features.hh.search.HHQuery` is the only
        hh-specific contract, and a future per-source flag should
        grow a typed field on it.
        """
        # Local import: ``features.hh.search`` does not depend on this
        # module, and pulling ``HHQuery`` at the top would create a
        # cycle the other direction.
        from apply_pilot.features.hh.search import HHQuery

        hh_query = HHQuery(
            text=query.text,
            area=query.area,
            salary=query.salary,
            page=query.page,
            per_page=query.per_page,
        )
        return await self._search_client.search(hh_query)

    def normalize(self, raw: dict[str, Any]) -> Vacancy:
        """Map ``raw`` to a canonical :class:`Vacancy` via the normaliser's hh branch."""
        return self._normalizer.normalize_hh(raw)

    def extract_screening_questions(self, raw: dict[str, Any]) -> list[ScreeningQuestion]:
        """Build screening-question rows for ``raw``.

        The screening extractor requires an existing :class:`Vacancy`
        (it stores ``vacancy_id`` on the row) — the adapter produces
        one by normalising ``raw`` first, then hands the pair to the
        extractor's
        :meth:`ScreeningQuestionExtractor.extract_from_vacancy`. This
        is the only place the adapter composes two collaborators on a
        single ``raw`` payload.
        """
        vacancy = self.normalize(raw)
        return self._screening_extractor.extract_from_vacancy(vacancy, raw)

    async def apply(self, job: ApplyJob) -> ApplyResult:
        """Submit ``job`` to the injected hh apply adapter.

        The adapter is the
        :class:`~apply_pilot.features.apply_worker.runtime.ApplyAdapter`
        Protocol — a ``name`` attribute plus an async
        :meth:`ApplyAdapter.submit`. Both
        :class:`~apply_pilot.features.hh.apply.HhApplyAdapter` (the
        production implementation) and the recording in-memory fake
        used in tests satisfy that contract.
        """
        return await self._apply_adapter.submit(job)


__all__ = ["HhSourceAdapter"]
