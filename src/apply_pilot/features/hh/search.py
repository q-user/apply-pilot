"""hh.ru vacancy search adapter (issue #22).

This module is the boundary between the application and hh.ru's public
vacancy search API. It exposes three things:

* :class:`HHQuery` — a value object describing the user-facing search
  filters, with built-in validation against hh.ru's hard limits.
* :class:`HHVacancySearchClient` — the :class:`typing.Protocol` every
  collaborator depends on. It is intentionally narrow: ``search`` and
  ``fetch_one`` only.
* :class:`InMemoryHhVacancySearchClient` — a dict-backed fake used by
  tests for the search service and any future cross-source workflow.
* :class:`HhHttpVacancySearchClient` — the production client that talks
  to ``https://api.hh.ru/vacancies`` via :mod:`httpx`.

The protocol-shaped return values are raw ``dict`` payloads: the canonical
mapping to :class:`~apply_pilot.features.sources.models.Vacancy` lives in
:class:`~apply_pilot.features.sources.normalizer.VacancyNormalizer`, which
keeps this module free of ORM coupling. The cross-source
:class:`VacancySearchService` in :mod:`apply_pilot.features.sources.search_service`
composes the two.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol

import httpx

from apply_pilot.shared.errors import DomainError

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class HHVacancySearchError(DomainError):
    """Base error for hh.ru vacancy search failures.

    Specialised into :class:`HHRateLimitError` for the ``429`` case so
    callers can choose to back off without inspecting the message.
    """

    code: str = "hh_vacancy_search_error"


class HHRateLimitError(HHVacancySearchError):
    """hh.ru returned HTTP 429 — the caller should back off and retry."""

    code: str = "hh_rate_limited"

    def __init__(self, message: str, *, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


class HHVacancyNotFoundError(HHVacancySearchError):
    """The requested hh vacancy id does not exist (HTTP 404)."""

    code: str = "hh_vacancy_not_found"


# ---------------------------------------------------------------------------
# HHQuery value object
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class HHQuery:
    """A user-facing search filter for hh.ru's vacancy search API.

    All fields are optional; ``text`` is the full-text search term, the
    rest narrow down the result set. Defaults follow hh.ru's recommended
    page size for unauthenticated calls (``per_page=50``).

    Validation is enforced in :meth:`__post_init__` so the type stays
    cheap to construct and easy to introspect.
    """

    #: hh.ru caps ``per_page`` at 100 on the public search endpoint.
    _MAX_PER_PAGE: int = 100

    text: str | None = None
    area: str | None = None
    salary: int | None = None
    page: int = 0
    per_page: int = 50

    def __post_init__(self) -> None:
        if self.page < 0:
            raise ValueError(f"page must be >= 0, got {self.page}")
        if not 1 <= self.per_page <= self._MAX_PER_PAGE:
            raise ValueError(f"per_page must be in [1, {self._MAX_PER_PAGE}], got {self.per_page}")
        if self.salary is not None and self.salary < 0:
            raise ValueError(f"salary must be >= 0, got {self.salary}")

    def to_query_params(self) -> dict[str, Any]:
        """Serialise the query into the ``/vacancies`` search parameter set.

        ``None``-valued filters are omitted so the request stays as small
        as possible (and so callers can introspect "what did we ask
        for?" from the URL alone).
        """
        params: dict[str, Any] = {"page": self.page, "per_page": self.per_page}
        if self.text is not None:
            params["text"] = self.text
        if self.area is not None:
            params["area"] = self.area
        if self.salary is not None:
            params["salary"] = self.salary
        return params


# ---------------------------------------------------------------------------
# Protocol
# ---------------------------------------------------------------------------


class HHVacancySearchClient(Protocol):
    """The narrow contract every hh search collaborator depends on.

    Return values are raw hh.ru payloads (``dict``) — callers are
    expected to feed them into
    :meth:`~apply_pilot.features.sources.normalizer.VacancyNormalizer.normalize_hh`
    for canonical mapping. Keeping the client format-agnostic means the
    same protocol can later back a different transport (e.g. cached) or
    a non-hh source.
    """

    async def search(self, query: HHQuery) -> list[dict]: ...

    async def fetch_one(self, hh_vacancy_id: str) -> dict: ...


# ---------------------------------------------------------------------------
# In-memory client
# ---------------------------------------------------------------------------


class InMemoryHhVacancySearchClient:
    """Dict-backed fake used by tests and local development.

    Fixtures are keyed by the search ``text`` they should be returned
    for; ``fetch_one`` looks the vacancy up by id across *all* fixtures
    regardless of the original search text. An unknown query yields an
    empty list (not an error) so the test surface matches hh.ru's
    zero-results behaviour.
    """

    def __init__(self, fixtures: dict[str, list[dict]] | None = None) -> None:
        self._fixtures: dict[str, list[dict]] = dict(fixtures or {})

    async def search(self, query: HHQuery) -> list[dict]:
        if query.text is None:
            # No text filter: return the union of all fixtures. Useful for
            # "give me everything" tests.
            return [item for items in self._fixtures.values() for item in items]
        return list(self._fixtures.get(query.text, []))

    async def fetch_one(self, hh_vacancy_id: str) -> dict:
        for items in self._fixtures.values():
            for item in items:
                if str(item.get("id")) == hh_vacancy_id:
                    return item
        raise HHVacancyNotFoundError(f"Vacancy {hh_vacancy_id!r} not found in in-memory fixtures")


# ---------------------------------------------------------------------------
# HTTP client
# ---------------------------------------------------------------------------


#: Resolves a bearer token for a given user, or returns ``None`` if no
#: credentials are stored. The signature is sync because the
#: ``HHCredentialService`` is sync; the wrapper makes it trivially
#: overridable in tests.
HHTokenProvider = Callable[[uuid.UUID], str | None]


def _noop_token_provider(_user_id: uuid.UUID) -> str | None:
    return None


class HhHttpVacancySearchClient:
    """Production :class:`HHVacancySearchClient` backed by :mod:`httpx`.

    The HTTP client is *injected* — callers (production wiring, tests)
    own its lifetime. This keeps the adapter testable with
    :class:`httpx.MockTransport` and lets the application share a
    pooled ``AsyncClient`` across slices.

    The public search endpoint does not require authentication, but
    hh.ru applies stricter rate limits to anonymous traffic. An
    optional ``token_provider`` lets callers attach a per-user
    ``Authorization: Bearer …`` header when credentials are available.
    """

    DEFAULT_USER_AGENT: str = "ApplyPilot/0.1"
    DEFAULT_BASE_URL: str = "https://api.hh.ru/vacancies"

    def __init__(
        self,
        *,
        client: httpx.AsyncClient,
        base_url: str = DEFAULT_BASE_URL,
        user_agent: str = DEFAULT_USER_AGENT,
        token_provider: HHTokenProvider | None = None,
    ) -> None:
        self._client = client
        self._base_url = base_url.rstrip("/")
        self._user_agent = user_agent
        self._token_provider: HHTokenProvider = token_provider or _noop_token_provider

    # ------------------------------------------------------------------
    # HHVacancySearchClient
    # ------------------------------------------------------------------

    async def search(self, query: HHQuery, *, user_id: uuid.UUID | None = None) -> list[dict]:
        """Fetch a page of vacancies matching ``query``.

        ``user_id`` is optional and is used only to attach a Bearer
        token via the configured ``token_provider``. The Protocol
        signature does not include it; the kwarg is allowed because
        Protocol structural matching is satisfied when the extra
        parameter is optional.
        """
        headers = self._build_headers(user_id=user_id)
        response = await self._client.get(
            self._base_url,
            params=query.to_query_params(),
            headers=headers,
        )
        return self._parse_search_response(response)

    async def fetch_one(self, hh_vacancy_id: str) -> dict:
        """Fetch a single vacancy by hh.ru id.

        Raises:
            HHVacancyNotFoundError: If hh.ru returns 404.
            HHRateLimitError: If hh.ru returns 429.
            HHVacancySearchError: For any other 4xx/5xx response.
        """
        response = await self._client.get(
            f"{self._base_url}/{hh_vacancy_id}",
            headers=self._build_headers(),
        )
        self._raise_for_status(response)
        return response.json()

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _build_headers(self, *, user_id: uuid.UUID | None = None) -> dict[str, str]:
        headers = {
            "User-Agent": self._user_agent,
            "Accept": "application/json",
        }
        if user_id is not None:
            token = self._token_provider(user_id)
            if token:
                headers["Authorization"] = f"Bearer {token}"
        return headers

    def _parse_search_response(self, response: httpx.Response) -> list[dict]:
        """Validate an hh.ru search response and return its ``items`` list."""
        self._raise_for_status(response)
        try:
            data = response.json()
        except ValueError as exc:
            # ``json.JSONDecodeError`` is a ``ValueError`` subclass; this
            # catches every malformed-body case the stdlib can raise.
            raise HHVacancySearchError(
                f"hh.ru returned a non-JSON body: {exc!s}",
            ) from exc
        items = data.get("items")
        if not isinstance(items, list):
            raise HHVacancySearchError(
                "hh.ru response is missing the 'items' array",
            )
        return items

    @staticmethod
    def _raise_for_status(response: httpx.Response) -> None:
        """Translate an hh.ru error into a typed domain error.

        The mapping is intentionally narrow:

        * ``429`` → :class:`HHRateLimitError` (with ``Retry-After``).
        * ``404`` → :class:`HHVacancyNotFoundError` (only meaningful for
          :meth:`fetch_one`; ``search`` will never hit this).
        * anything else ``>= 400`` → :class:`HHVacancySearchError`.
        """
        status = response.status_code
        if status == httpx.codes.TOO_MANY_REQUESTS:
            retry_after_raw = response.headers.get("Retry-After")
            retry_after: float | None
            try:
                retry_after = float(retry_after_raw) if retry_after_raw else None
            except ValueError:
                retry_after = None
            raise HHRateLimitError(
                f"hh.ru rate limited the request (HTTP {status})",
                retry_after=retry_after,
            )
        if status == httpx.codes.NOT_FOUND:
            raise HHVacancyNotFoundError(
                f"hh.ru returned 404 for {response.url!s}",
            )
        if status >= 400:
            raise HHVacancySearchError(
                f"hh.ru search failed with HTTP {status}: {response.text[:200]!r}",
            )


__all__ = [
    "HHQuery",
    "HHRateLimitError",
    "HHTokenProvider",
    "HHVacancyNotFoundError",
    "HHVacancySearchClient",
    "HHVacancySearchError",
    "HhHttpVacancySearchClient",
    "InMemoryHhVacancySearchClient",
]
