"""Robust parser for the LLM's response.

Real LLM responses drift away from the strict JSON contract documented
in the prompt: some are wrapped in Markdown fences, some carry trailing
commas, some omit optional fields, some return scores outside the
``[0, 100]`` range. The parser is the safety net that turns whatever
the model actually returned into a :class:`~.scorer.ScoreResult` — or
raises :class:`LLMScoreParseError` when the output is unparseable.

The parser is intentionally tolerant: clamping happens here so callers
never have to special-case an out-of-range score. Optional fields fall
back to documented defaults so a missing ``confidence`` or
``explanation`` does not blow up the pipeline.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

# ---------------------------------------------------------------------------
# Value object: ScoreResult
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ScoreResult:
    """The verdict of the LLM scorer on a single ``(vacancy, profile)`` pair.

    Lives in this module so the parser can construct it without
    importing the scorer (which would be a circular import). The
    scorer re-uses the same dataclass and just stamps the
    ``prompt_version`` field.

    Attributes
    ----------
    score:
        Integer in ``[0, 100]`` produced by the LLM (clamped on parse).
    explanation:
        Free-text justification the LLM returned. Empty string when
        the model did not provide one.
    prompt_version:
        The version of the prompt template that was used. Set by the
        scorer after the registry lookup; empty in the raw parser
        output.
    confidence:
        Float in ``[0.0, 1.0]`` the LLM returned to indicate its own
        certainty. Defaults to ``1.0`` when the model did not provide
        one. Used by callers for downstream filtering, not by the
        scorer itself.
    """

    score: int
    explanation: str
    prompt_version: str
    confidence: float = 1.0


# ---------------------------------------------------------------------------
# Error
# ---------------------------------------------------------------------------


class LLMScoreParseError(ValueError):
    """Raised when the LLM's response cannot be turned into a ScoreResult.

    The original raw response is preserved in ``raw`` for logging and
    diagnostics.
    """

    def __init__(self, message: str, *, raw: str = "") -> None:
        super().__init__(message)
        self.raw = raw

    def __str__(self) -> str:
        # Include the raw response (truncated) in the str form so log
        # lines and exception messages carry enough context to debug
        # the upstream LLM without having to re-fetch the raw value
        # from the exception attribute.
        if self.raw:
            snippet = self.raw if len(self.raw) <= 200 else self.raw[:200] + "..."
            return f"{super().__str__()}: {snippet}"
        return super().__str__()


#: Minimum and maximum allowed scores. Anything outside is clamped.
_SCORE_MIN: int = 0
_SCORE_MAX: int = 100

#: Sentinel used in :class:`InMemoryLLMClient` to mean "any prompt".
WILDCARD_PROMPT: str = "*"

#: Regex used to strip Markdown ```json ... ``` fences. The optional
#: language tag is captured but not used — we accept ``json``, ``JSON``
#: or no language at all.
_FENCE_RE = re.compile(
    r"```(?:json|JSON)?\s*(?P<body>.*?)\s*```",
    re.DOTALL,
)


def _strip_fences(raw: str) -> str:
    """Strip Markdown ```...``` fences around the JSON payload.

    The regex is non-greedy and only matches when the response contains
    a fence — responses without fences are returned unchanged (modulo
    the whitespace strip below).
    """
    match = _FENCE_RE.search(raw)
    if match is not None:
        return match.group("body").strip()
    return raw.strip()


def _extract_object(raw: str) -> str:
    """Return the substring from the first ``{`` to the matching ``}``.

    LLMs sometimes add prose before the JSON object. We locate the
    first ``{`` and trust the trailing brace to close the object.
    The function does *not* balance nested braces — that's the JSON
    parser's job. We only isolate the candidate substring.
    """
    start = raw.find("{")
    if start == -1:
        return raw
    end = raw.rfind("}")
    if end == -1 or end < start:
        return raw[start:]
    return raw[start : end + 1]


def _parse_json(raw: str) -> dict[str, Any]:
    """Parse ``raw`` as JSON, tolerating trailing commas.

    The trailing-comma regex strips a comma followed by ``}`` or ``]`` —
    a common LLM artefact that the standard library rejects outright.
    """
    cleaned = re.sub(r",(\s*[}\]])", r"\1", raw)
    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError as exc:
        raise LLMScoreParseError(
            f"LLM response is not valid JSON: {exc.msg}",
            raw=raw,
        ) from exc
    if not isinstance(data, dict):
        raise LLMScoreParseError(
            f"LLM response is not a JSON object: got {type(data).__name__}",
            raw=raw,
        )
    return data


def _coerce_score(value: object, *, raw: str) -> int:
    """Coerce ``value`` to an ``int`` score in ``[0, 100]``.

    Numeric strings (``"85"``) are accepted; anything else raises
    :class:`LLMScoreParseError`. The result is clamped to the allowed
    range.
    """
    if isinstance(value, bool):
        # ``bool`` is a subclass of ``int`` in Python — guard against
        # ``True``/``False`` being treated as ``1``/``0``.
        raise LLMScoreParseError(f"score must be numeric, got bool: {value!r}", raw=raw)
    if isinstance(value, (int, float)):
        score = int(value)
    elif isinstance(value, str):
        try:
            score = int(value.strip())
        except ValueError as exc:
            raise LLMScoreParseError(f"score is not a number: {value!r}", raw=raw) from exc
    else:
        raise LLMScoreParseError(f"score has unexpected type {type(value).__name__}", raw=raw)
    if score < _SCORE_MIN:
        return _SCORE_MIN
    if score > _SCORE_MAX:
        return _SCORE_MAX
    return score


def _coerce_confidence(value: object | None) -> float:
    """Coerce ``value`` to a float confidence in ``[0.0, 1.0]``.

    Missing or ``None`` → ``1.0`` (max confidence). A non-numeric value
    falls back to ``1.0`` rather than failing the parse — confidence
    is an optional signal, not a hard contract.
    """
    if value is None:
        return 1.0
    if isinstance(value, bool):
        # ``True``/``False`` → ``1.0``/``0.0`` respectively, since
        # they are sensible defaults for a missing confidence.
        return 1.0 if value else 0.0
    if isinstance(value, (int, float)):
        conf = float(value)
    elif isinstance(value, str):
        try:
            conf = float(value)
        except ValueError:
            return 1.0
    else:
        return 1.0
    if conf < 0.0:
        return 0.0
    if conf > 1.0:
        return 1.0
    return conf


def _coerce_explanation(value: object | None) -> str:
    """Coerce ``value`` to a string explanation.

    Missing or ``None`` → empty string. Non-string values are coerced
    via :class:`str` so ``42`` becomes ``"42"`` — better than dropping
    the field entirely.
    """
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    return str(value)


def parse_score_response(raw: str) -> ScoreResult:
    """Parse the LLM's response string into a :class:`ScoreResult`.

    The function is tolerant of common LLM artefacts:

    * Markdown ```json ... ``` fences are stripped.
    * Trailing commas in objects and arrays are removed.
    * Extra prose around the JSON object is ignored (the first ``{``
      starts the parse).
    * A numeric score string (``"85"``) is coerced to ``85``.
    * ``score`` outside ``[0, 100]`` is clamped.
    * Missing ``confidence`` defaults to ``1.0``; out-of-range
      confidence is clamped to ``[0.0, 1.0]``.
    * Missing ``explanation`` defaults to ``""``.

    Raises
    ------
    LLMScoreParseError
        When the response is empty, is not JSON, is not a JSON object,
        or is missing the mandatory ``score`` field. The original raw
        response is attached to the exception as ``raw``.
    """
    if not raw or not raw.strip():
        raise LLMScoreParseError("LLM response is empty", raw=raw)

    body = _strip_fences(raw)
    if not body:
        raise LLMScoreParseError("LLM response is empty after stripping fences", raw=raw)

    candidate = _extract_object(body)
    if "{" not in candidate:
        raise LLMScoreParseError("LLM response contains no JSON object", raw=raw)

    data = _parse_json(candidate)
    if "score" not in data:
        raise LLMScoreParseError("LLM response is missing required 'score' field", raw=raw)

    score = _coerce_score(data["score"], raw=raw)
    explanation = _coerce_explanation(data.get("explanation"))
    confidence = _coerce_confidence(data.get("confidence"))

    return ScoreResult(
        score=score,
        explanation=explanation,
        prompt_version="",  # filled in by the scorer
        confidence=confidence,
    )


__all__ = [
    "LLMScoreParseError",
    "ScoreResult",
    "WILDCARD_PROMPT",
    "parse_score_response",
]
