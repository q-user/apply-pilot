"""Failing tests for :func:`parse_score_response` (issue #29).

The LLM scoring pipeline asks a chat model to score a ``(vacancy,
profile)`` pair. The model is instructed to reply with a strict JSON
object, but real-world responses drift: some are wrapped in Markdown
fences, some carry trailing commas, some omit optional fields, some
return scores outside ``[0, 100]``. The parser is the safety net that
turns whatever the model actually returned into a usable
:class:`ScoreResult` — or raises :class:`LLMScoreParseError` when the
output is unparseable.
"""

from __future__ import annotations

import json

import pytest

from job_apply.features.scoring.parsing import (
    LLMScoreParseError,
    parse_score_response,
)
from job_apply.features.scoring.scorer import ScoreResult

# ---------------------------------------------------------------------------
# Plain JSON
# ---------------------------------------------------------------------------


class TestParsePlainJson:
    def test_parses_well_formed_response(self) -> None:
        raw = json.dumps(
            {
                "score": 87,
                "explanation": "Strong match on the must-have skills.",
                "confidence": 0.9,
            }
        )

        result = parse_score_response(raw)

        assert isinstance(result, ScoreResult)
        assert result.score == 87
        assert result.explanation == "Strong match on the must-have skills."
        assert result.confidence == 0.9

    def test_missing_confidence_defaults_to_one(self) -> None:
        """Confidence is optional; missing → 1.0 (max confidence)."""
        raw = json.dumps({"score": 50, "explanation": "ok"})

        result = parse_score_response(raw)

        assert result.score == 50
        assert result.confidence == 1.0

    def test_missing_explanation_defaults_to_empty_string(self) -> None:
        raw = json.dumps({"score": 30})

        result = parse_score_response(raw)

        assert result.score == 30
        assert result.explanation == ""

    def test_missing_score_raises(self) -> None:
        """``score`` is mandatory; an LLM that omits it is broken."""
        raw = json.dumps({"explanation": "no score given"})

        with pytest.raises(LLMScoreParseError):
            parse_score_response(raw)


# ---------------------------------------------------------------------------
# Markdown-fenced JSON
# ---------------------------------------------------------------------------


class TestParseFencedJson:
    def test_strips_json_fence(self) -> None:
        raw = (
            "```json\n"
            + json.dumps({"score": 42, "explanation": "fenced response", "confidence": 0.6})
            + "\n```"
        )

        result = parse_score_response(raw)

        assert result.score == 42
        assert result.explanation == "fenced response"
        assert result.confidence == 0.6

    def test_strips_plain_fence_without_language(self) -> None:
        raw = "```\n" + json.dumps({"score": 10, "explanation": "x"}) + "\n```"

        result = parse_score_response(raw)

        assert result.score == 10

    def test_strips_surrounding_whitespace(self) -> None:
        raw = "\n\n  " + json.dumps({"score": 5, "explanation": "y"}) + "  \n"

        result = parse_score_response(raw)

        assert result.score == 5


# ---------------------------------------------------------------------------
# Trailing commas / non-standard JSON
# ---------------------------------------------------------------------------


class TestParseNonStandardJson:
    def test_tolerates_trailing_comma_in_object(self) -> None:
        raw = '{"score": 70, "explanation": "trailing",}'

        result = parse_score_response(raw)

        assert result.score == 70
        assert result.explanation == "trailing"

    def test_tolerates_extra_text_around_json(self) -> None:
        """A common LLM behaviour is to add a leading sentence before
        the JSON block. The parser should locate the first ``{`` and
        try to parse from there."""
        raw = 'Here is my verdict:\n{"score": 25, "explanation": "weak"}'

        result = parse_score_response(raw)

        assert result.score == 25


# ---------------------------------------------------------------------------
# Score clamping
# ---------------------------------------------------------------------------


class TestScoreClamping:
    @pytest.mark.parametrize("raw_score,expected", [(150, 100), (-10, 0), (0, 0), (100, 100)])
    def test_score_is_clamped_to_zero_one_hundred(self, raw_score: int, expected: int) -> None:
        raw = json.dumps({"score": raw_score, "explanation": "x"})

        result = parse_score_response(raw)

        assert result.score == expected

    def test_numeric_score_as_string_is_parsed(self) -> None:
        """Some models return ``"85"`` (string) instead of ``85`` (int).
        The parser coerces numeric strings so the score is still usable."""
        raw = json.dumps({"score": "85", "explanation": "string score"})

        result = parse_score_response(raw)

        assert result.score == 85
        assert isinstance(result.score, int)


# ---------------------------------------------------------------------------
# Confidence clamping
# ---------------------------------------------------------------------------


class TestConfidenceClamping:
    def test_confidence_above_one_is_clamped(self) -> None:
        raw = json.dumps({"score": 50, "explanation": "x", "confidence": 1.7})

        result = parse_score_response(raw)

        assert result.confidence == 1.0

    def test_confidence_below_zero_is_clamped(self) -> None:
        raw = json.dumps({"score": 50, "explanation": "x", "confidence": -0.3})

        result = parse_score_response(raw)

        assert result.confidence == 0.0


# ---------------------------------------------------------------------------
# Invalid input
# ---------------------------------------------------------------------------


class TestParseFailures:
    def test_garbage_raises(self) -> None:
        with pytest.raises(LLMScoreParseError):
            parse_score_response("not json at all")

    def test_empty_string_raises(self) -> None:
        with pytest.raises(LLMScoreParseError):
            parse_score_response("")

    def test_json_without_object_raises(self) -> None:
        with pytest.raises(LLMScoreParseError):
            parse_score_response("[1, 2, 3]")

    def test_non_numeric_score_raises(self) -> None:
        raw = json.dumps({"score": "high", "explanation": "x"})

        with pytest.raises(LLMScoreParseError):
            parse_score_response(raw)

    def test_parse_error_message_includes_raw(self) -> None:
        raw = "{not even close to json}"

        with pytest.raises(LLMScoreParseError) as excinfo:
            parse_score_response(raw)

        # The error should carry some context for debugging; we don't
        # assert the exact format — just that the raw input is there.
        assert "not even close" in str(excinfo.value)
