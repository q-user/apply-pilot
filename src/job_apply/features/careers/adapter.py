"""``SourceAdapter`` implementation for company-careers-page sources.

The :class:`CareersPageSourceAdapter` is the careers-flavored
implementation of the cross-source
:class:`~job_apply.features.sources.adapter.SourceAdapter` Protocol.
One instance is built per configured :class:`CareersPageSite`; the
instance's :attr:`name` is ``"careers:<site.name>"`` so the
:class:`~job_apply.features.sources.adapter.AdapterRegistry` can
hold a dozen of them side-by-side without key collisions.

Responsibilities
----------------

The adapter is the only place that knows about a *single* company's
careers page. It composes three narrow collaborators:

* :class:`CareersHttpClient` — the HTTP transport. Injected so tests
  use :class:`InMemoryCareersHttpClient` and production uses
  :class:`HttpCareersClient` (an :class:`httpx.Client` wrapper).
* The parsers (:func:`parse_rss` / :func:`parse_html`) — applied to
  the response body based on the site kind. The parser surface is
  intentionally tiny; the issue is about the adapter contract.
* :class:`~job_apply.features.sources.normalizer.VacancyNormalizer`
  — the same normaliser every other source uses. The adapter tags
  each raw dict with the configured ``parser_id`` and the site
  ``name`` so the normaliser can dispatch on the right branch.

Retry policy
------------

The adapter implements the per-site retry policy itself — a
:class:`RetryPolicy` is the wrong tool because the policy is local
to one HTTP call (not a queue) and the per-site tuning lives in
:class:`CareersPageSite`. The contract is:

* Transient errors (5xx, transport errors) are retried up to
  ``site.retry_count - 1`` times (the initial call counts as the
  first attempt).
* 4xx errors are permanent and propagate immediately.
* The delay between attempts is
  ``site.retry_backoff_seconds * 2 ** (attempt - 1)``, an
  exponential backoff in the same spirit as the apply worker.

The :class:`asyncio.sleep` call is the only side effect outside the
HTTP call; tests can override it by monkey-patching
``asyncio.sleep`` if they want to exercise timing.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import httpx

from job_apply.features.apply_worker.models import ApplyJob
from job_apply.features.apply_worker.runtime import ApplyResult
from job_apply.features.careers.client import (
    CareersHttpClient,
    CareersHTTPError,
    CareersTransportError,
)
from job_apply.features.careers.config import CareersPageSite
from job_apply.features.careers.parser import parse_payload
from job_apply.features.screening.models import ScreeningQuestion
from job_apply.features.sources.adapter import SourceQuery
from job_apply.features.sources.models import Vacancy
from job_apply.features.sources.normalizer import VacancyNormalizer
from job_apply.shared.errors import DomainError

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class CareersAdapterError(DomainError):
    """Base error for the careers adapter.

    A single error type keeps callers' ``except`` clauses simple:
    both retry-exhaustion and 4xx propagation surface as the same
    class. The :attr:`status_code` attribute is populated when the
    failure came from a non-2xx response; ``None`` for transport /
    parsing errors.
    """

    code: str = "careers_adapter_error"

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


# ---------------------------------------------------------------------------
# Adapter
# ---------------------------------------------------------------------------


class CareersPageSourceAdapter:
    """A :class:`SourceAdapter` for one company-careers-page.

    The adapter is constructed once per configured site and
    registered in the :class:`AdapterRegistry` under
    ``SourceAdapter.name`` (i.e. ``careers:<site.name>``). The
    constructor takes the three collaborators it needs and the
    site description that drives its behaviour — there is no
    global state to wire.

    The adapter ignores most of the :class:`SourceQuery` it
    receives. The :class:`SourceAdapter` contract is source-agnostic
    and hh.ru-flavored (``text``/``area``/``salary``); a careers
    page is a fixed URL, so the only meaningful "search" is "fetch
    the page and return whatever is there". ``SourceQuery.page``
    is honoured as a sanity no-op (logged but unused) so the call
    sites that always pass a :class:`SourceQuery` do not have to
    branch on the source.
    """

    #: Prefix used in :attr:`name` to namespace the adapter key in
    #: the :class:`AdapterRegistry`. ``"careers:acme"``,
    #: ``"careers:globex"`` and ``"careers:initech"`` can co-exist
    #: without colliding with ``"hh"``.
    NAME_PREFIX: str = "careers:"

    def __init__(
        self,
        *,
        site: CareersPageSite,
        http_client: CareersHttpClient,
        normalizer: VacancyNormalizer | None = None,
    ) -> None:
        self._site = site
        self._http_client = http_client
        self._normalizer = normalizer or VacancyNormalizer()

    # ------------------------------------------------------------------
    # SourceAdapter protocol surface
    # ------------------------------------------------------------------

    @property
    def name(self) -> str:
        """Stable identifier in the format ``"careers:<site.name>"``."""
        return f"{self.NAME_PREFIX}{self._site.name}"

    @property
    def site(self) -> CareersPageSite:
        """Return the per-site configuration (read-only)."""
        return self._site

    async def search(self, query: SourceQuery) -> list[dict[str, Any]]:
        """Fetch the configured URL with retry and return parsed items.

        The :class:`SourceQuery` is accepted for protocol compliance
        but only ``page`` and ``per_page`` are inspected (and
        currently ignored — the adapter is a fixed-URL fetcher).

        Returns:
            A list of raw vacancy dicts. The shape is:

            .. code-block:: python

                {
                    "id": "<stable id>",
                    "title": "<display title>",
                    "url": "<absolute URL>",
                    "description": "<optional>",
                    "published_at": "<optional, raw RFC-822>",
                    "parser_id": "<site.parser_id>",
                    "employer_name": "<site.name>",
                }

        Raises:
            CareersAdapterError: when the retry budget is exhausted
                on transient errors, or when the page returns a
                non-2xx / non-5xx response (i.e. 4xx).
        """
        del query  # unused — careers pages are a fixed URL
        body = await self._fetch_with_retry()
        items = parse_payload(self._site.kind, body, base_url=self._base_url())
        return [self._tag(item) for item in items]

    def normalize(self, raw: dict[str, Any]) -> Vacancy:
        """Map a raw careers-page dict to a canonical :class:`Vacancy`.

        The normaliser is the same one every other source uses; the
        adapter is responsible only for tagging the dict with the
        right ``source`` and ``employer_name`` before delegating.

        The :class:`VacancyNormalizer` does not (yet) have a
        ``careers`` branch, so the adapter builds the :class:`Vacancy`
        directly. This keeps the cross-source change minimal and
        the careers-specific mapping co-located with the adapter.
        """
        return _normalize_careers(raw, source=self.name, employer_name=self._site.name)

    def extract_screening_questions(self, raw: dict[str, Any]) -> list[ScreeningQuestion]:
        """Return an empty list — career pages have no structured questions.

        The :class:`SourceAdapter` Protocol requires the method; the
        careers slice keeps the slot but never populates it. A
        future per-site extension could fill the list from a
        follow-up detail-page scrape.
        """
        del raw
        return []

    async def apply(self, job: ApplyJob) -> ApplyResult:
        """Always raise — careers pages do not support programmatic apply.

        The :class:`ApplyWorker` catches :class:`NotImplementedError`
        and dead-letters the row, so the slice does not need a
        separate "is_applyable" flag (see
        :meth:`SourceAdapter.apply`).
        """
        del job
        raise NotImplementedError(
            f"Careers page source {self.name!r} does not support programmatic apply."
        )

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _base_url(self) -> str:
        """Return the site URL stripped of its path/query — the parser's origin.

        The HTML parser uses :func:`urllib.parse.urljoin` to absolutise
        relative ``href`` values, and ``urljoin`` needs the document
        URL (path included) to behave intuitively. We pass the
        configured URL verbatim and let ``urljoin`` strip the path
        when it encounters an absolute href.
        """
        return self._site.url

    def _tag(self, raw: dict[str, Any]) -> dict[str, Any]:
        """Stamp ``parser_id`` and ``employer_name`` on a raw dict.

        The normaliser reads these keys; stamping them here keeps
        the parsers free of cross-source policy.
        """
        raw["parser_id"] = self._site.parser_id
        raw["employer_name"] = self._site.name
        return raw

    async def _fetch_with_retry(self) -> str:
        """GET the configured URL with exponential-backoff retry.

        Retries are attempted on transient errors only: 5xx
        responses and :class:`CareersTransportError` /
        :class:`httpx.HTTPError`. 4xx propagates immediately.
        """
        url = self._site.url
        max_attempts = self._site.retry_count
        backoff = self._site.retry_backoff_seconds
        last_error: Exception | None = None

        for attempt in range(1, max_attempts + 1):
            try:
                response = self._http_client.get(url)
            except CareersTransportError as exc:
                last_error = exc
                logger.warning(
                    "Careers fetch transport error site=%s attempt=%d/%d err=%s",
                    self._site.name,
                    attempt,
                    max_attempts,
                    exc,
                )
            except httpx.HTTPError as exc:
                # Map httpx's transport errors into our domain error
                # so the retry logic has a single exception to catch.
                last_error = CareersTransportError(str(exc) or exc.__class__.__name__)
                logger.warning(
                    "Careers fetch httpx error site=%s attempt=%d/%d err=%s",
                    self._site.name,
                    attempt,
                    max_attempts,
                    exc,
                )
            else:
                if response.status_code >= 500:
                    last_error = CareersHTTPError(
                        f"careers page returned {response.status_code}",
                        status_code=response.status_code,
                        retryable=True,
                    )
                    logger.warning(
                        "Careers fetch %d site=%s attempt=%d/%d",
                        response.status_code,
                        self._site.name,
                        attempt,
                        max_attempts,
                    )
                elif response.status_code >= 400:
                    raise CareersAdapterError(
                        f"careers page returned {response.status_code} for url={url}",
                        status_code=response.status_code,
                    )
                else:
                    return response.text

            if attempt < max_attempts:
                # Exponential backoff: ``backoff * 2 ** (attempt - 1)``.
                # No jitter — the slice is a small fetch, not a queue.
                delay = backoff * (2 ** (attempt - 1))
                await asyncio.sleep(delay)

        # All attempts exhausted.
        message = (
            f"careers page fetch failed after {max_attempts} attempts for site={self._site.name}"
        )
        if last_error is not None:
            message = f"{message}: {last_error}"
        raise CareersAdapterError(message)


# ---------------------------------------------------------------------------
# Normalisation helper
# ---------------------------------------------------------------------------


def _normalize_careers(raw: dict[str, Any], *, source: str, employer_name: str) -> Vacancy:
    """Build a :class:`Vacancy` from a tagged careers-page raw dict.

    The adapter stamps ``source`` (``"careers:<name>"``) and
    ``employer_name`` (``<name>``) onto the raw dict before
    delegating here. The :class:`VacancyNormalizer` does not (yet)
    have a ``careers`` branch; this module-local helper keeps the
    careers-specific mapping in one place. When the cross-source
    normaliser grows a ``careers`` branch, the body of this
    function can move there verbatim.

    The ``content_hash`` is derived from the canonical triple
    (title, description, employer) so cross-source dedup catches a
    vacancy scraped from a careers page *and* from a job board.
    """
    from job_apply.features.sources.normalizer import _compute_content_hash

    title = str(raw.get("title") or "")
    description = raw.get("description")
    url = raw.get("url")
    source_id = str(raw.get("id") or "")
    return Vacancy(
        source=source,
        source_id=source_id,
        title=title,
        description=str(description) if description is not None else None,
        url=str(url) if url is not None else None,
        # Salaries are not extracted in the MVP — career pages do
        # not carry structured salary data and the regex parser
        # would only mis-read the page chrome.
        salary_from=None,
        salary_to=None,
        salary_currency="RUR",
        salary_gross=False,
        employer_name=employer_name,
        location=None,
        schedule=None,
        experience=None,
        skills=None,
        published_at=None,
        source_updated_at=None,
        raw_data=dict(raw),
        content_hash=_compute_content_hash(title, description, employer_name),
    )


__all__ = [
    "CareersAdapterError",
    "CareersPageSourceAdapter",
]
