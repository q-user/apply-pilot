"""HTTP transport for the careers adapter (M7, issue #59).

The adapter is HTTP-bound (a careers page is fetched with a plain
GET), so the slice owns a tiny :class:`CareersHttpClient` Protocol
plus two implementations:

* :class:`InMemoryCareersHttpClient` — dict-backed fake used by
  tests. Supports both a single pre-registered response per URL
  (the common case) and a *queue* of responses so retry tests can
  feed ``[503, 503, 200]`` and observe the round-trip.
* :class:`HttpCareersClient` — production wrapper around an
  injected :class:`httpx.Client`. The injected client follows the
  same DI convention as the hh slice (see
  :class:`~apply_pilot.features.hh.search.HhHttpVacancySearchClient`):
  callers (production wiring, tests) own the client's lifetime, and
  tests use :class:`httpx.MockTransport` to keep the network fake.

Errors
------

The slice raises :class:`CareersTransportError` for transport-level
failures (connection reset, timeout, DNS failure) and
:class:`CareersHTTPError` for non-2xx responses. The adapter's
retry loop catches the transport error and the 5xx subclass of
:class:`CareersHTTPError`; 4xx is permanent and propagates.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from typing import Protocol, cast

import httpx

from apply_pilot.shared.errors import DomainError

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class CareersTransportError(DomainError):
    """The HTTP transport failed — connection reset, timeout, DNS, etc.

    The adapter's retry loop treats this as transient: it consumes
    one attempt and may try again.
    """

    code: str = "careers_transport_error"


class CareersHTTPError(DomainError):
    """A non-2xx response that the adapter considers non-retryable.

    The default mapping is: ``4xx`` → non-retryable, ``5xx`` →
    retryable. The adapter raises this exception *after* the retry
    budget is exhausted; for a single attempt the response is
    returned and inspected by the adapter.
    """

    code: str = "careers_http_error"

    def __init__(self, message: str, *, status_code: int, retryable: bool) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.retryable = retryable


# ---------------------------------------------------------------------------
# Protocol
# ---------------------------------------------------------------------------


class CareersHttpClient(Protocol):
    """The narrow HTTP contract the adapter depends on.

    Returns the raw :class:`httpx.Response` so the adapter (or its
    caller) can branch on ``status_code`` and ``text``. The
    Protocol is structural; anything that exposes
    :meth:`get` returning an :class:`httpx.Response` satisfies it.
    """

    def get(self, url: str) -> httpx.Response: ...


# ---------------------------------------------------------------------------
# In-memory client
# ---------------------------------------------------------------------------


class InMemoryCareersHttpClient:
    """Dict-backed fake used by tests.

    The fake stores *per-URL* state in two slots:

    * ``responses`` — a single :class:`httpx.Response` (the common
      case) **or** a list of responses consumed in order (the retry
      case). When a list is registered, each call pops the head.
    * ``programmatic_errors`` — a list of
      :class:`CareersTransportError` instances raised on the
      corresponding attempts *before* the response queue is
      consulted. The two slots compose so a test can model
      ``[TransportError, TransportError, 200]`` cleanly.
    * ``call_counts`` — ``dict[str, int]`` of observed calls,
      exposed via :meth:`call_count` for assertions.

    A missing URL raises :class:`KeyError` (not a domain error) so
    tests can spot a typo in the fixture without a swallowed
    exception.
    """

    def __init__(
        self,
        *,
        responses: dict[str, httpx.Response | list[httpx.Response]] | None = None,
    ) -> None:
        self.responses: dict[str, httpx.Response | list[httpx.Response]] = dict(responses or {})
        self.programmatic_errors: dict[str, list[Exception]] = {}
        self.call_counts: dict[str, int] = {}

    def get(self, url: str) -> httpx.Response:
        """Return the next pre-registered response for ``url``.

        Raises:
            KeyError: if ``url`` was never registered.
            CareersTransportError: if a programmatic error is queued
                for the current attempt.
        """
        self.call_counts[url] = self.call_counts.get(url, 0) + 1
        attempt = self.call_counts[url] - 1
        queued_errors = self.programmatic_errors.get(url) or []
        if attempt < len(queued_errors):
            raise queued_errors[attempt]
        if url not in self.responses:
            raise KeyError(url)
        payload = self.responses[url]
        if isinstance(payload, list):
            if not payload:
                # The list was exhausted on a prior call; treat the
                # missing entry as "no more pre-registered responses".
                raise KeyError(url)
            return cast(httpx.Response, payload.pop(0))
        return payload

    def call_count(self, url: str) -> int:
        """Return the number of :meth:`get` calls observed for ``url``."""
        return self.call_counts.get(url, 0)

    def queue_responses(self, url: str, responses: Iterable[httpx.Response]) -> None:
        """Register a list of responses to be returned in order."""
        self.responses[url] = list(responses)


# ---------------------------------------------------------------------------
# Production client
# ---------------------------------------------------------------------------


class HttpCareersClient:
    """Production :class:`CareersHttpClient` backed by :class:`httpx.Client`.

    The :class:`httpx.Client` is injected — callers (production
    wiring, tests) own its lifetime. This keeps the adapter
    testable with :class:`httpx.MockTransport` and lets the
    application share a pooled client across slices.

    The :class:`httpx.Response` is returned as-is; status code
    inspection and exception translation are the adapter's job.
    """

    DEFAULT_USER_AGENT: str = "ApplyPilot/0.19 (careers)"

    def __init__(
        self,
        client: httpx.Client,
        *,
        user_agent: str = DEFAULT_USER_AGENT,
    ) -> None:
        self._client = client
        self._user_agent = user_agent

    def get(self, url: str) -> httpx.Response:
        """Perform a single GET against ``url``.

        Network-level failures (connection reset, timeout) propagate
        as :class:`httpx.HTTPError` subclasses; the adapter's retry
        loop catches them as transient.
        """
        logger.debug("Fetching careers page url=%s", url)
        return self._client.get(url, headers={"User-Agent": self._user_agent})


__all__ = [
    "CareersHTTPError",
    "CareersHttpClient",
    "CareersTransportError",
    "HttpCareersClient",
    "InMemoryCareersHttpClient",
]
