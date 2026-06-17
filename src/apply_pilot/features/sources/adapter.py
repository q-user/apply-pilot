"""Source-adapter interface (M7, issue #70).

The :class:`SourceAdapter` Protocol is the unified boundary between the
application and an external job source (hh.ru today; Habr Career,
Telegram channels, company sites in future). It exposes the full source
lifecycle in one place:

* :meth:`SourceAdapter.search` — fetch raw vacancy dicts.
* :meth:`SourceAdapter.normalize` — turn a raw dict into a canonical
  :class:`~apply_pilot.features.sources.models.Vacancy`.
* :meth:`SourceAdapter.extract_screening_questions` — build
  :class:`~apply_pilot.features.screening.models.ScreeningQuestion` rows
  from a raw dict.
* :meth:`SourceAdapter.apply` — submit an application (optional; some
  sources do not support programmatic apply).

Why a single Protocol
---------------------

Before issue #70 each hh.ru collaborator lived in its own module
(``features.hh.search`` for the search client, ``features.sources.normalizer``
for the normalizer, ``features.screening.extractor`` for the screening
extractor, ``features.hh.apply`` for the apply adapter). The
``SourceService`` and ``ApplyWorker`` wired them together by hand. Adding
a new source (Habr Career, Telegram) meant writing four sibling classes
and remembering to compose them in the right places.

The :class:`SourceAdapter` Protocol captures the lifecycle as a single
type so a new source can be added with one class. The cross-source
orchestration code (the future ``MigrateSearch`` workflow, the apply
worker) looks adapters up by :attr:`name` in an
:class:`AdapterRegistry` instead of importing the right collaborator
from each feature package.

The hh.ru implementation lives in
:mod:`apply_pilot.features.hh.adapter` (``HhSourceAdapter``); this module
owns the cross-source types — the Protocol, the value object and the
registry.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from apply_pilot.features.apply_worker.models import ApplyJob
from apply_pilot.features.screening.models import ScreeningQuestion
from apply_pilot.features.sources.models import Vacancy

if TYPE_CHECKING:
    # ``ApplyResult`` lives in :mod:`apply_pilot.features.apply_worker.runtime`,
    # which transitively imports :mod:`apply_pilot.features.sources.models`. The
    # Protocol below only references ``ApplyResult`` in a type annotation, so
    # we import it under :data:`typing.TYPE_CHECKING` to keep the import graph
    # acyclic at runtime.
    from apply_pilot.features.apply_worker.runtime import ApplyResult

# ---------------------------------------------------------------------------
# SourceQuery value object
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SourceQuery:
    """A source-agnostic search filter.

    All fields are optional. The first three (``text``, ``area``,
    ``salary``) are the common filter set every job board exposes; the
    pagination fields (``page`` / ``per_page``) follow hh.ru's
    conventional defaults. The :attr:`extra` dict is the source-specific
    extension point: keys like ``"only_with_salary"`` (hh) or
    ``"schedule"`` (habr) live there so the dataclass does not have to
    grow a field for every per-source flag.

    Adapters translate this value object into their own query type
    (e.g. :class:`~apply_pilot.features.hh.search.HHQuery` for the hh
    adapter) before calling the underlying search client.
    """

    text: str | None = None
    area: str | None = None
    salary: int | None = None
    page: int = 0
    per_page: int = 50
    #: Source-specific extension point. Default-factory so each instance
    #: gets its own dict and there is no shared-mutable-state footgun.
    extra: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# SourceAdapter Protocol
# ---------------------------------------------------------------------------


@runtime_checkable
class SourceAdapter(Protocol):
    """The unified boundary every external job source implements.

    The Protocol is :func:`typing.runtime_checkable` so the
    :class:`AdapterRegistry` (and any future cross-source
    orchestration code) can use ``isinstance(adapter, SourceAdapter)``
    as a structural check. Methods are documented per-implementation
    so a reader does not have to follow the Protocol to understand a
    specific source.

    Attributes
    ----------
    name:
        Stable source identifier (``"hh"``, ``"habr"``, ...). It is
        the key the rest of the application uses to look adapters up
        in an :class:`AdapterRegistry`, and the value the
        :attr:`~apply_pilot.features.sources.models.Vacancy.source`
        column carries — the two must match.
    """

    name: str

    async def search(self, query: SourceQuery) -> list[dict[str, Any]]:
        """Fetch raw vacancy dicts that match ``query``.

        The return value is a list of source-specific payloads (the
        "raw" form). Callers feed each dict into
        :meth:`SourceAdapter.normalize` to get a canonical
        :class:`Vacancy`.
        """

    def normalize(self, raw: dict[str, Any]) -> Vacancy:
        """Map a raw vacancy dict into a canonical :class:`Vacancy`."""

    def extract_screening_questions(self, raw: dict[str, Any]) -> list[ScreeningQuestion]:
        """Build :class:`ScreeningQuestion` rows from a raw vacancy dict.

        Implementations are free to compose a normaliser + a
        source-specific extractor (the hh adapter does exactly that)
        or to extract directly from the raw payload.
        """

    async def apply(self, job: ApplyJob) -> ApplyResult:
        """Submit an application for ``job`` to the external system.

        Optional on the protocol: sources that do not support
        programmatic apply raise :class:`NotImplementedError`. The
        :class:`ApplyWorker` catches that exception and dead-letters
        the job, so the slice does not need a separate "is_applyable"
        flag.
        """


# ---------------------------------------------------------------------------
# AdapterRegistry
# ---------------------------------------------------------------------------


class AdapterRegistry:
    """In-memory index of :class:`SourceAdapter` instances keyed by :attr:`name`.

    The cross-source orchestration code (the future
    ``VacancySearchService`` migration, the :class:`ApplyWorker`)
    looks adapters up here instead of importing the right collaborator
    from each feature package. Adapters are registered at startup
    (e.g. from :mod:`apply_pilot.app`) and looked up by name at request
    time.

    Lookup methods are non-throwing: an unknown name yields ``None``
    (or an empty list) so callers can branch on "no such adapter"
    without a try/except. Registration is a no-op when the same name
    is registered twice — the most recent adapter wins, but tests
    typically register a fresh registry per scenario.
    """

    def __init__(self) -> None:
        self._adapters: dict[str, SourceAdapter] = {}

    def register(self, adapter: SourceAdapter) -> None:
        """Register ``adapter`` under its :attr:`name`."""
        self._adapters[adapter.name] = adapter

    def get(self, name: str) -> SourceAdapter | None:
        """Return the adapter registered under ``name``, or ``None``."""
        return self._adapters.get(name)

    def list(self) -> list[str]:
        """Return the names of every registered adapter, in insertion order."""
        return list(self._adapters.keys())


__all__ = [
    "AdapterRegistry",
    "SourceAdapter",
    "SourceQuery",
]
