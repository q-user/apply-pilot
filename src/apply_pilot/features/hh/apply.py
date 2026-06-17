"""hh.ru apply submission adapter (M5, issue #48).

This module is the boundary between the apply worker and hh.ru's
``POST /negotiations`` endpoint. It implements the
:class:`~apply_pilot.features.apply_worker.runtime.ApplyAdapter` Protocol
keyed by ``"hh"`` in the worker's adapter registry, and it is the
single place that attaches the ``Idempotency-Key`` header required by
issue #48.

Actors and responsibilities
---------------------------

* :class:`HhApplyAdapter` — production :class:`ApplyAdapter` backed by
  :mod:`httpx`. The transport is *injected* so tests can plug in
  :class:`httpx.MockTransport`; production wiring passes a pooled
  :class:`httpx.AsyncClient` and lets the worker own its lifetime.

Idempotency-Key contract
------------------------

The apply worker re-enqueues the same :class:`ApplyJob` on transient
failures (see :mod:`apply_pilot.features.apply_worker.retry`). hh.ru
deduplicates ``POST /negotiations`` calls that share an
``Idempotency-Key`` header, so the adapter must forward
``ApplyJob.idempotency_key`` verbatim on every retry — otherwise the
second call would create a *second* application for the same
``(user, vacancy, match)`` triple.

Body
----

The body is intentionally minimal: ``{"vacancy_id": str(job.vacancy_id)}``.
The resume id and cover-letter text are not part of the M5 #48 contract
and will be injected by a future slice (the production wiring
:mod:`apply_pilot.app` builds the adapter with a payload builder).
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Callable
from typing import Any

import httpx

from apply_pilot.features.apply_worker.models import ApplyJob
from apply_pilot.features.apply_worker.runtime import ApplyResult
from apply_pilot.shared.errors import DomainError

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class HhApplyError(DomainError):
    """Base error for hh.ru apply submission failures.

    Specialised into :class:`HhApplyRateLimitError` for the ``429`` case
    so the worker can back off without inspecting the message.
    """

    code: str = "hh_apply_error"


class HhApplyRateLimitError(HhApplyError):
    """hh.ru returned HTTP 429 — the caller should back off and retry."""

    code: str = "hh_apply_rate_limited"

    def __init__(self, message: str, *, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


# ---------------------------------------------------------------------------
# Token resolution
# ---------------------------------------------------------------------------


#: Resolves a bearer token for a given user, or returns ``None`` if no
#: credentials are stored. The signature is sync because the
#: ``HHCredentialService`` is sync; the wrapper makes it trivially
#: overridable in tests and in production wiring.
HhApplyTokenProvider = Callable[[uuid.UUID], str | None]


def _noop_token_provider(_user_id: uuid.UUID) -> str | None:
    """Default :data:`HhApplyTokenProvider` that always returns ``None``."""
    return None


# ---------------------------------------------------------------------------
# Adapter
# ---------------------------------------------------------------------------


class HhApplyAdapter:
    """Production :class:`ApplyAdapter` for hh.ru (issue #48).

    The transport is injectable — callers (production wiring, tests)
    own its lifetime. This keeps the adapter testable with
    :class:`httpx.MockTransport` and lets the application share a
    pooled :class:`httpx.AsyncClient` across slices.

    An optional ``token_provider`` attaches a per-user
    ``Authorization: Bearer …`` header when credentials are available.
    The M5 #48 contract is *only* about the ``Idempotency-Key`` header,
    so token attachment is opt-in.
    """

    DEFAULT_USER_AGENT: str = "ApplyPilot/0.1"
    DEFAULT_BASE_URL: str = "https://api.hh.ru/negotiations"

    #: The :class:`ApplyAdapter` discriminator. The worker's adapter
    #: registry is keyed by ``vacancy.source``; this attribute is the
    #: companion string the worker uses to pick the right adapter.
    name: str = "hh"

    def __init__(
        self,
        *,
        client: httpx.AsyncClient,
        base_url: str = DEFAULT_BASE_URL,
        user_agent: str = DEFAULT_USER_AGENT,
        token_provider: HhApplyTokenProvider | None = None,
    ) -> None:
        self._client = client
        self._base_url = base_url.rstrip("/")
        self._user_agent = user_agent
        self._token_provider: HhApplyTokenProvider = token_provider or _noop_token_provider

    # ------------------------------------------------------------------
    # ApplyAdapter
    # ------------------------------------------------------------------

    async def submit(self, job: ApplyJob) -> ApplyResult:
        """POST the apply request to hh.ru and translate the response.

        The :class:`ApplyJob.idempotency_key` is forwarded as the
        ``Idempotency-Key`` header — the same value on retries, so
        hh.ru can dedup. A successful response yields
        :class:`ApplyResult` with ``external_application_id`` set to
        hh.ru's ``negotiation id``; any non-2xx response yields a
        non-retryable :class:`ApplyResult` (or a retryable one for
        ``429``).
        """
        headers = self._build_headers(job)
        body = self._build_body(job)
        try:
            response = await self._client.post(self._base_url, json=body, headers=headers)
        except httpx.HTTPError as exc:
            # Network / timeout / connection error — let the worker treat
            # this as a transient failure and retry with the same
            # Idempotency-Key.
            return ApplyResult(
                success=False,
                external_application_id=None,
                error=f"hh_apply_transport_error: {exc}",
                retryable=True,
            )
        return self._parse_response(response)

    async def aclose(self) -> None:
        """Close the underlying HTTP client.

        Adapters that own their client (test fixtures that build a
        one-shot transport) call this in test teardown; production
        wiring passes a long-lived client and never calls ``aclose``.
        """
        await self._client.aclose()

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _build_headers(self, job: ApplyJob) -> dict[str, str]:
        """Assemble the outgoing request headers.

        ``Idempotency-Key`` is the issue #48 contract — it must be the
        slice-stable SHA-256 of ``(user, vacancy, match)`` so hh.ru
        deduplicates retries of the same logical application.
        """
        headers: dict[str, str] = {
            "User-Agent": self._user_agent,
            "Accept": "application/json",
            "Content-Type": "application/json",
            "Idempotency-Key": job.idempotency_key,
        }
        token = self._token_provider(job.user_id)
        if token:
            headers["Authorization"] = f"Bearer {token}"
        return headers

    def _build_body(self, job: ApplyJob) -> dict[str, Any]:
        """Build the apply body.

        For the M5 #48 contract only ``vacancy_id`` is required; future
        slices that need ``resume_id`` / ``message`` will inject a
        payload builder into the adapter constructor rather than mutate
        the body here.
        """
        return {"vacancy_id": str(job.vacancy_id)}

    def _parse_response(self, response: httpx.Response) -> ApplyResult:
        """Translate an hh.ru response into an :class:`ApplyResult`.

        The mapping mirrors the search client:

        * ``429`` → retryable failure, ``Retry-After`` parsed when
          present.
        * any other ``>= 400`` → non-retryable failure (the request
          reached hh.ru; retrying with the same ``Idempotency-Key``
          would not change the outcome).
        * ``2xx`` → success, ``external_application_id`` extracted from
          the ``id`` field of the JSON body.
        """
        status = response.status_code
        if status == httpx.codes.TOO_MANY_REQUESTS:
            retry_after_raw = response.headers.get("Retry-After")
            retry_after: float | None
            try:
                retry_after = float(retry_after_raw) if retry_after_raw else None
            except ValueError:
                retry_after = None
            raise HhApplyRateLimitError(
                f"hh.ru rate limited the apply request (HTTP {status})",
                retry_after=retry_after,
            )
        if status >= 400:
            return ApplyResult(
                success=False,
                external_application_id=None,
                error=f"hh_apply_error: HTTP {status}: {response.text[:200]!r}",
                retryable=False,
            )
        try:
            data = response.json()
        except ValueError as exc:
            return ApplyResult(
                success=False,
                external_application_id=None,
                error=f"hh_apply_error: non-JSON body: {exc}",
                retryable=False,
            )
        negotiation_id = data.get("id")
        if not isinstance(negotiation_id, str) or not negotiation_id:
            return ApplyResult(
                success=False,
                external_application_id=None,
                error="hh_apply_error: missing 'id' in response",
                retryable=False,
            )
        return ApplyResult(
            success=True,
            external_application_id=negotiation_id,
            error=None,
            retryable=False,
        )


__all__ = [
    "HhApplyAdapter",
    "HhApplyError",
    "HhApplyRateLimitError",
    "HhApplyTokenProvider",
]
