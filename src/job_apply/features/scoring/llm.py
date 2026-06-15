"""LLM client, response parser, and orchestrator for the M3 deep scoring slice.

This single module owns the entire LLM-facing surface for
``features/scoring`` (issue #29):

* :class:`LLMSettings` — env-driven configuration for the HTTP client.
* :class:`LLMClient` — duck-typed Protocol the scorer depends on.
* :class:`InMemoryLLMClient` — dict-backed fake used by the test suite.
* :class:`HttpLLMClient` — ``httpx``-backed client speaking the
  OpenAI-compatible ``/v1/chat/completions`` endpoint. Tests inject a
  :class:`httpx.MockTransport` so no real network call is made.
* :class:`LLMScoreParseError` / :func:`parse_score_response` — tolerant
  parser that handles JSON, fenced JSON, trailing commas, and clamps
  out-of-range numbers to their valid interval.
* :class:`ScoreResult` — the value object the scorer returns.
* :class:`LLMScorer` — the orchestrator that builds a prompt, calls the
  LLM, parses the response, and stamps it with the prompt version.

The module deliberately keeps prompt construction in
:mod:`.prompts` and persistence orchestration in
:mod:`.service`; this file is only the LLM-facing layer.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

import httpx

from job_apply.features.scoring.prompts import (
    VACANCY_SCORING_PROMPT_VERSION,
    build_vacancy_scoring_prompt,
)

# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class LLMSettings:
    """Configuration for the :class:`HttpLLMClient`.

    Environment variables:

    * ``APP_LLM_API_KEY`` (required for production use) — the bearer
      token sent in the ``Authorization`` header.
    * ``APP_LLM_BASE_URL`` (optional, default ``"https://api.openai.com/v1"``)
      — the OpenAI-compatible endpoint root; the client appends
      ``/chat/completions``.
    * ``APP_LLM_MODEL`` (optional, default ``"gpt-4o-mini"``) — the
      model name passed in the request body.

    The dataclass is frozen so the settings are hashable and cannot be
    mutated after construction; the scorer is a long-lived singleton
    and accidental mutation would silently change every subsequent
    request.
    """

    api_key: str
    base_url: str = "https://api.openai.com/v1"
    model: str = "gpt-4o-mini"

    def __post_init__(self) -> None:
        if not self.api_key:
            raise ValueError(
                "LLMSettings.api_key must be a non-empty string; "
                "set the APP_LLM_API_KEY environment variable."
            )
        if not self.base_url:
            raise ValueError("LLMSettings.base_url must be a non-empty string")
        if not self.model:
            raise ValueError("LLMSettings.model must be a non-empty string")


def get_llm_settings() -> LLMSettings:
    """Build :class:`LLMSettings` from the environment.

    Raises:
        ValueError: If ``APP_LLM_API_KEY`` is unset or empty. The check
            is eager so a misconfigured deployment fails at startup
            rather than at the first LLM call.
    """
    api_key = os.getenv("APP_LLM_API_KEY", "").strip()
    if not api_key:
        raise ValueError("APP_LLM_API_KEY environment variable must be set to a non-empty value.")
    return LLMSettings(
        api_key=api_key,
        base_url=os.getenv("APP_LLM_BASE_URL", "https://api.openai.com/v1").strip()
        or "https://api.openai.com/v1",
        model=os.getenv("APP_LLM_MODEL", "gpt-4o-mini").strip() or "gpt-4o-mini",
    )


# ---------------------------------------------------------------------------
# LLM client Protocol
# ---------------------------------------------------------------------------


@runtime_checkable
class LLMClient(Protocol):
    """The minimal duck-typed surface the :class:`LLMScorer` depends on.

    Two implementations live in this module:

    * :class:`InMemoryLLMClient` — tests inject a pre-loaded dict.
    * :class:`HttpLLMClient` — production; speaks the
      OpenAI-compatible ``/v1/chat/completions`` endpoint over
      :mod:`httpx`.
    """

    async def complete(
        self,
        prompt: str,
        *,
        temperature: float = 0.2,
        max_tokens: int = 1024,
    ) -> str:
        """Return the model's response text for ``prompt``."""
        ...


# ---------------------------------------------------------------------------
# In-memory implementation
# ---------------------------------------------------------------------------


ResponseMap = Mapping[str, str | Callable[[str], str]]


#: Sentinel key on :class:`InMemoryLLMClient` that matches any prompt.
#: Tests that don't care about the exact prompt content can register a
#: single response under this key and have it returned for every call.
WILDCARD_PROMPT: str = "*"


class InMemoryLLMClient:
    """Dict-backed LLM fake for tests.

    The mapping is ``prompt -> response``. Each value can be a static
    ``str`` or a ``Callable[[str], str]``; the callable form lets tests
    capture the exact prompt the scorer produced and assert on its
    content. A missing key raises :class:`KeyError` — a loud failure
    beats a silent default.

    The sentinel key :data:`WILDCARD_PROMPT` (``"*"``) matches any
    prompt; it's intended for tests that don't need to assert on the
    prompt content (e.g. ``ScoringService`` tests) and would otherwise
    have to thread the full rendered prompt through the fixture.
    """

    __slots__ = ("_responses",)

    def __init__(self, *, responses: ResponseMap) -> None:
        self._responses: ResponseMap = dict(responses)

    async def complete(
        self,
        prompt: str,
        *,
        temperature: float = 0.2,
        max_tokens: int = 1024,
    ) -> str:
        value = self._responses.get(WILDCARD_PROMPT)
        if value is not None:
            return value(prompt) if callable(value) else value
        if prompt not in self._responses:
            raise KeyError(f"InMemoryLLMClient has no response for prompt: {prompt[:80]!r}...")
        value = self._responses[prompt]
        return value(prompt) if callable(value) else value


# ---------------------------------------------------------------------------
# HTTP implementation
# ---------------------------------------------------------------------------


class HttpLLMClient:
    """OpenAI-compatible chat-completions client built on :mod:`httpx`.

    The client holds a single :class:`httpx.AsyncClient` for the
    lifetime of the instance and uses an injectable
    :class:`httpx.MockTransport` in tests so no real network traffic
    ever leaves the process.
    """

    __slots__ = ("_client", "_model", "_url")

    def __init__(
        self,
        settings: LLMSettings,
        *,
        transport: httpx.MockTransport | None = None,
    ) -> None:
        # ``base_url`` is the API root; the chat-completions endpoint
        # is one level below it.
        self._url = settings.base_url.rstrip("/") + "/chat/completions"
        self._model = settings.model
        # The MockTransport is wired via httpx's documented testing
        # hook: passing it as the ``transport=`` argument short-circuits
        # the network stack entirely. When no transport is supplied we
        # use the default one — that's the production path.
        self._client = httpx.AsyncClient(
            headers={"Authorization": f"Bearer {settings.api_key}"},
            timeout=httpx.Timeout(30.0),
            transport=transport,
        )

    async def __aenter__(self) -> HttpLLMClient:
        return self

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        await self._client.aclose()

    async def aclose(self) -> None:
        """Close the underlying :class:`httpx.AsyncClient`."""
        await self._client.aclose()

    async def complete(
        self,
        prompt: str,
        *,
        temperature: float = 0.2,
        max_tokens: int = 1024,
    ) -> str:
        """Send ``prompt`` to the chat-completions endpoint and return the text.

        Raises:
            httpx.HTTPStatusError: The server returned a non-2xx response.
            RuntimeError: The 2xx response is missing the ``choices[0].message.content`` field.
        """
        response = await self._client.post(
            self._url,
            json={
                "model": self._model,
                "temperature": temperature,
                "max_tokens": max_tokens,
                "messages": [{"role": "user", "content": prompt}],
            },
        )
        response.raise_for_status()
        payload = response.json()
        choices = payload.get("choices") or []
        if not choices:
            raise RuntimeError(f"LLM response contained no choices: {payload!r}")
        return choices[0]["message"]["content"]


# ---------------------------------------------------------------------------
# Response parsing
# ---------------------------------------------------------------------------


class LLMScoreParseError(ValueError):
    """Raised when an LLM response cannot be turned into a :class:`ScoreResult`."""


_FENCE_RE = re.compile(r"^```(?:json)?\s*|```\s*$", re.MULTILINE)
_TRAILING_COMMA_RE = re.compile(r",(\s*[}\]])")


def _strip_fences(raw: str) -> str:
    """Remove Markdown code fences (```json ... ```) wrapping the JSON body."""
    return _FENCE_RE.sub("", raw).strip()


def _strip_trailing_commas(raw: str) -> str:
    """Drop the JSON-invalid trailing commas ``,`` before ``}``/``]``."""
    return _TRAILING_COMMA_RE.sub(r"\1", raw)


def _coerce_score(value: Any) -> int:
    """Coerce ``value`` to an int in ``[0, 100]``; raise on failure."""
    if isinstance(value, bool):
        # ``bool`` is a subclass of ``int`` in Python; reject it so
        # ``true``/``false`` from the LLM is a parse error, not a 0/1.
        raise LLMScoreParseError(f"score must be a number, got bool: {value!r}")
    if not isinstance(value, int | float):
        raise LLMScoreParseError(f"score must be a number, got {type(value).__name__}")
    if value != value:  # NaN guard; ``float('nan') != float('nan')``.
        raise LLMScoreParseError("score is NaN")
    return max(0, min(100, int(round(value))))


def _coerce_confidence(value: Any) -> float:
    """Coerce ``value`` to a float in ``[0.0, 1.0]``; raise on failure."""
    if isinstance(value, bool):
        raise LLMScoreParseError(f"confidence must be a number, got bool: {value!r}")
    if not isinstance(value, int | float):
        raise LLMScoreParseError(f"confidence must be a number, got {type(value).__name__}")
    if value != value:
        raise LLMScoreParseError("confidence is NaN")
    return max(0.0, min(1.0, float(value)))


def _coerce_explanation(value: Any) -> str:
    """Coerce ``value`` to a string; ``None`` becomes the empty string."""
    if value is None:
        return ""
    if not isinstance(value, str):
        return str(value)
    return value


@dataclass(frozen=True, slots=True)
class ScoreResult:
    """The value object the LLM scoring pipeline returns.

    Attributes
    ----------
    score:
        Integer in ``[0, 100]``. The parser clamps out-of-range values
        to this interval; it never raises for a numeric value outside
        the band.
    explanation:
        1-3 sentence justification produced by the LLM. Empty string
        when the LLM omitted the field.
    prompt_version:
        Version stamp of the prompt template the scorer used. Set by
        :class:`LLMScorer` from
        :data:`~job_apply.features.scoring.prompts.VACANCY_SCORING_PROMPT_VERSION`.
    confidence:
        Float in ``[0.0, 1.0]`` from the LLM, defaulting to ``1.0`` when
        absent. Clamped to the valid interval by the parser.
    """

    score: int
    explanation: str
    prompt_version: str
    confidence: float = 1.0


def parse_score_response(raw: str) -> ScoreResult:
    """Parse a raw LLM response string into a :class:`ScoreResult`.

    The parser is intentionally tolerant:

    * Markdown ````json ... ```` fences are stripped.
    * Trailing commas before ``}``/``]`` are removed.
    * Out-of-range numeric ``score``/``confidence`` are clamped to
      their valid interval rather than rejected.
    * Missing optional fields (``explanation``, ``confidence``,
      ``prompt_version``) fall back to safe defaults.

    Raises:
        LLMScoreParseError: The input cannot be turned into a JSON
            object, the ``score`` field is missing, or the ``score``
            is not numeric.
    """
    if raw is None:
        raise LLMScoreParseError("LLM response is None")
    cleaned = _strip_fences(raw.strip())
    cleaned = _strip_trailing_commas(cleaned)
    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError as exc:
        raise LLMScoreParseError(f"LLM response is not valid JSON: {exc.msg}") from exc
    if not isinstance(data, dict):
        raise LLMScoreParseError(f"LLM response must be a JSON object, got {type(data).__name__}")
    if "score" not in data:
        raise LLMScoreParseError("LLM response is missing the 'score' field")
    score = _coerce_score(data["score"])
    confidence_value = data.get("confidence", 1.0)
    confidence = _coerce_confidence(confidence_value) if confidence_value is not None else 1.0
    explanation = _coerce_explanation(data.get("explanation"))
    prompt_version = _coerce_explanation(data.get("prompt_version"))
    return ScoreResult(
        score=score,
        explanation=explanation,
        prompt_version=prompt_version,
        confidence=confidence,
    )


# ---------------------------------------------------------------------------
# Scorer
# ---------------------------------------------------------------------------


class LLMScorer:
    """Orchestrates one LLM scoring call.

    The scorer depends on a duck-typed :class:`LLMClient` rather than
    a concrete implementation, so tests can swap in
    :class:`InMemoryLLMClient` and production wires
    :class:`HttpLLMClient`.
    """

    __slots__ = ("_llm",)

    def __init__(self, llm: LLMClient) -> None:
        self._llm = llm

    async def score(
        self,
        vacancy: Any,
        profile: Any,
        *,
        resume_text: str | None = None,
    ) -> ScoreResult:
        """Score ``vacancy`` against ``profile`` and return a :class:`ScoreResult`.

        The LLM is expected to return a strict JSON object that
        :func:`parse_score_response` can decode. The scorer's
        ``prompt_version`` is set from the canonical
        :data:`~job_apply.features.scoring.prompts.VACANCY_SCORING_PROMPT_VERSION`
        constant — the LLM's own ``prompt_version`` field, if any, is
        discarded.
        """
        prompt = build_vacancy_scoring_prompt(vacancy, profile, resume_text=resume_text)
        raw = await self._llm.complete(prompt)
        result = parse_score_response(raw)
        return ScoreResult(
            score=result.score,
            explanation=result.explanation,
            prompt_version=VACANCY_SCORING_PROMPT_VERSION,
            confidence=result.confidence,
        )


__all__ = [
    "HttpLLMClient",
    "InMemoryLLMClient",
    "LLMClient",
    "LLMScoreParseError",
    "LLMScorer",
    "LLMSettings",
    "ScoreResult",
    "WILDCARD_PROMPT",
    "get_llm_settings",
    "parse_score_response",
]
