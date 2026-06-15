"""LLM client implementations for the scoring vertical slice (issue #29).

Two implementations live here:

* :class:`InMemoryLLMClient` — dict- or function-backed fake used by
  tests. No I/O; the response is whatever the test configured.
* :class:`HttpLLMClient` — production client that talks to an
  OpenAI-compatible ``/v1/chat/completions`` endpoint over ``httpx``.

Both satisfy the :class:`LLMClient` Protocol declared in
:mod:`.scorer`. Settings are a frozen dataclass built from the
environment (``APP_LLM_API_KEY``, ``APP_LLM_BASE_URL``,
``APP_LLM_MODEL``); the default model is ``gpt-4o-mini`` per the
project's spec.

Network isolation in tests
--------------------------

:class:`HttpLLMClient` accepts an ``httpx.AsyncClient`` (or a
``httpx.MockTransport`` for tests). Tests *never* hit a real
endpoint — they use ``httpx.MockTransport`` so the request is
captured and a synthetic response is returned synchronously.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

import httpx

from job_apply.features.scoring.parsing import WILDCARD_PROMPT
from job_apply.features.scoring.scorer import LLMClient

#: Re-export the LLM client Protocol so callers can depend on the
#: ``llm`` module alone when wiring the slice. The Protocol's
#: canonical home is :mod:`.scorer` because that is where the
#: runtime_checkable decoration is paired with the scorer's needs.
__all__ = [
    "HttpLLMClient",
    "InMemoryLLMClient",
    "InMemoryResponses",
    "LLMClient",
    "LLMSettings",
]

# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------


#: Default ``model`` value when ``APP_LLM_MODEL`` is not set.
_DEFAULT_MODEL: str = "gpt-4o-mini"

#: Default ``base_url`` when ``APP_LLM_BASE_URL`` is not set.
_DEFAULT_BASE_URL: str = "https://api.openai.com"


@dataclass(frozen=True, slots=True)
class LLMSettings:
    """Configuration for the LLM HTTP client.

    Attributes
    ----------
    api_key:
        The bearer token sent in the ``Authorization`` header. Empty
        by default; the client will fail at request time if the key
        is empty rather than at construction, so unconfigured
        deployments produce a clear error in the first LLM call.
    base_url:
        The OpenAI-compatible endpoint root. The client appends
        ``/v1/chat/completions``. No trailing slash is required.
    model:
        The model identifier sent in the request body. Defaults to
        ``"gpt-4o-mini"`` per the project spec.
    """

    api_key: str
    base_url: str = _DEFAULT_BASE_URL
    model: str = _DEFAULT_MODEL

    @classmethod
    def from_env(cls) -> LLMSettings:
        """Build settings from ``APP_LLM_*`` environment variables.

        Reads:

        * ``APP_LLM_API_KEY`` (required at request time, not here).
        * ``APP_LLM_BASE_URL`` (default: ``"https://api.openai.com"``).
        * ``APP_LLM_MODEL`` (default: ``"gpt-4o-mini"``).

        Returns a :class:`LLMSettings` with the resolved values. The
        ``api_key`` defaults to an empty string so unconfigured
        deployments produce a clear failure at request time rather
        than at settings construction.
        """
        return cls(
            api_key=os.getenv("APP_LLM_API_KEY", ""),
            base_url=os.getenv("APP_LLM_BASE_URL", _DEFAULT_BASE_URL),
            model=os.getenv("APP_LLM_MODEL", _DEFAULT_MODEL),
        )


# ---------------------------------------------------------------------------
# InMemoryLLMClient
# ---------------------------------------------------------------------------


#: Type alias for the in-memory responses container. Either a dict
#: mapping exact prompt → response string, or a callable that
#: receives the prompt and returns the response. The alias is
#: declared as :data:`typing.Any` at the type-checker level because
#: a precise union of ``Mapping | Callable`` triggers a soundness
#: issue with the runtime ``callable(...)`` dispatch — we let the
#: runtime check, not the type-checker, decide.
InMemoryResponses = Any


class InMemoryLLMClient:
    """Dict- or function-backed fake LLM client.

    The client satisfies the :class:`LLMClient` Protocol. Two modes:

    * **dict mode** — ``responses={"hello": "world"}`` returns
      ``"world"`` for the prompt ``"hello"``. A special key
      :data:`~job_apply.features.scoring.parsing.WILDCARD_PROMPT`
      (``"*"``) matches any prompt. A missing prompt raises
      :class:`KeyError` so a test fixture mismatch is loud.
    * **function mode** — ``responses=lambda prompt, **kw: ...`` lets
      the test inspect the prompt, the temperature, the max tokens,
      and any other kwargs the scorer passed.

    The client also exposes a synchronous :meth:`complete_sync` helper
    so tests that don't want to ``await`` can use it directly.
    """

    __slots__ = ("_responses",)

    def __init__(self, responses: InMemoryResponses) -> None:
        self._responses = responses

    async def complete(
        self,
        prompt: str,
        *,
        temperature: float = 0.2,
        max_tokens: int = 1024,
    ) -> str:
        """Async protocol method — see class docstring for behaviour."""
        return self._resolve(prompt)

    def complete_sync(
        self,
        prompt: str,
        *,
        temperature: float = 0.2,
        max_tokens: int = 1024,
    ) -> str:
        """Synchronous helper for tests that don't want to ``await``."""
        return self._resolve(prompt)

    def _resolve(self, prompt: str) -> str:
        responses = self._responses
        # ``callable(...)`` returns ``False`` for :class:`Mapping`
        # instances, so this dispatch is safe: a dict falls through
        # to the wildcard/exact-key branches; a callable is invoked
        # with the prompt.
        if callable(responses):
            return responses(prompt)  # type: ignore[operator]
        if WILDCARD_PROMPT in responses:  # type: ignore[operator]
            return responses[WILDCARD_PROMPT]  # type: ignore[index]
        if prompt in responses:  # type: ignore[operator]
            return responses[prompt]  # type: ignore[index]
        raise KeyError(f"InMemoryLLMClient has no response for prompt: {prompt!r}")


# ---------------------------------------------------------------------------
# HttpLLMClient
# ---------------------------------------------------------------------------


class HttpLLMClient:
    """Production LLM client backed by ``httpx``.

    The client posts to ``{base_url}/v1/chat/completions`` with an
    ``Authorization: Bearer {api_key}`` header and an OpenAI-compatible
    request body. The response's ``choices[0].message.content`` is
    returned to the caller.

    Parameters
    ----------
    settings:
        A :class:`LLMSettings` carrying the ``api_key``, ``base_url``
        and ``model``. Use :meth:`LLMSettings.from_env` to read from
        the environment.
    client:
        Optional pre-built :class:`httpx.AsyncClient`. Production
        callers can pass one configured with a connection pool;
        tests pass one wired to :class:`httpx.MockTransport`. When
        omitted, the client builds its own with a 30s timeout.
    transport:
        Optional :class:`httpx.MockTransport` for tests. The
        constructor forwards this to the underlying ``AsyncClient``
        so tests can stub network calls without monkey-patching.

    The class does **not** retry on transient errors; the caller
    decides. The single-flight call shape keeps the slice easy to
    reason about — retries belong in a wrapper above the slice, not
    inside the LLM client.
    """

    __slots__ = ("_client", "_owns_client", "_settings")

    #: The relative path appended to ``base_url`` to form the
    #: completions endpoint.
    COMPLETIONS_PATH: str = "/v1/chat/completions"

    def __init__(
        self,
        settings: LLMSettings,
        *,
        client: httpx.AsyncClient | None = None,
        transport: httpx.MockTransport | None = None,
    ) -> None:
        self._settings = settings
        if client is not None:
            self._client = client
            self._owns_client = False
            return
        # Build our own ``AsyncClient``. We avoid ``**kwargs`` so the
        # type-checker can verify the parameter types against
        # ``httpx.AsyncClient.__init__``'s signature (which is
        # heavily overloaded).
        if transport is not None:
            self._client = httpx.AsyncClient(transport=transport, timeout=httpx.Timeout(30.0))
        else:
            self._client = httpx.AsyncClient(timeout=httpx.Timeout(30.0))
        self._owns_client = True

    @property
    def settings(self) -> LLMSettings:
        """Return the injected settings (read-only)."""
        return self._settings

    async def aclose(self) -> None:
        """Close the underlying :class:`httpx.AsyncClient` if we own it."""
        if self._owns_client:
            await self._client.aclose()

    async def __aenter__(self) -> HttpLLMClient:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    async def complete(
        self,
        prompt: str,
        *,
        temperature: float = 0.2,
        max_tokens: int = 1024,
    ) -> str:
        """Send ``prompt`` to the LLM and return the response string.

        Raises
        ------
        httpx.HTTPStatusError
            The endpoint returned a 4xx/5xx status. The caller decides
            whether to retry.
        KeyError
            The response body is missing ``choices[0].message.content``.
            This signals a misbehaving provider or a contract change;
            we let the error propagate so the operator can investigate.
        """
        url = self._settings.base_url.rstrip("/") + self.COMPLETIONS_PATH
        headers = {
            "Authorization": f"Bearer {self._settings.api_key}",
            "Content-Type": "application/json",
        }
        body = {
            "model": self._settings.model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        response = await self._client.post(url, headers=headers, json=body)
        response.raise_for_status()
        data = response.json()
        try:
            return data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise KeyError(f"LLM response missing choices[0].message.content: {data!r}") from exc
