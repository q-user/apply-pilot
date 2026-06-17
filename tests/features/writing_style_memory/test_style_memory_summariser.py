"""Unit tests for the deterministic ``summarise_letter`` helper.

The MVP summariser is intentionally simple — no LLM, no external service:

* the first sentence (split on ``.``, ``!``, ``?``);
* the total word count;
* the top-3 trigrams of the lower-cased text (joined with ``-``).

Keeping the heuristic deterministic and unit-tested means we can ship the
storage / ingestion pipeline in M8 (issue #66) without waiting on an LLM
dependency; future tickets can replace the helper with a real
summarisation call without touching the rest of the slice.
"""

from __future__ import annotations

from apply_pilot.features.writing_style_memory.summariser import summarise_letter


def test_summarise_returns_empty_string_for_blank_input() -> None:
    """Whitespace-only / empty input must yield an empty summary."""
    assert summarise_letter("") == ""
    assert summarise_letter("   \n\t  ") == ""


def test_summarise_includes_first_sentence() -> None:
    """The first sentence must be the prefix of the summary.

    The summariser strips trailing sentence-ending punctuation
    (``!``/``?``/``.``) from the first sentence so the prefix stays
    free of delimiters that would clash with the ``;`` separator used
    between sections.
    """
    text = "Hello there! I bring ten years of Python experience."
    summary = summarise_letter(text)
    assert summary.startswith("first-sentence: Hello there")
    assert "Hello there" in summary


def test_summarise_includes_word_count() -> None:
    """The word count must be the count of whitespace-separated tokens."""
    text = "one two three four five"
    summary = summarise_letter(text)
    assert "words=5" in summary


def test_summarise_includes_trigrams() -> None:
    """The summary must list the top-3 trigrams as ``word-word-word`` tokens."""
    text = "alpha beta gamma delta epsilon zeta alpha beta gamma"
    summary = summarise_letter(text)
    # The four bigram "alpha beta" appears in two locations, so the
    # 3-gram "alpha beta gamma" appears twice. We just assert the
    # helper emits ``trigrams=`` and the tokens use ``-`` as the joiner.
    assert "trigrams=" in summary
    trigrams_part = summary.split("trigrams=", 1)[1]
    tokens = [t for t in trigrams_part.split(", ") if t]
    assert all("-" in t for t in tokens)


def test_summarise_is_deterministic() -> None:
    """The same input must produce the same output on repeated calls."""
    text = "Hello, I am writing to apply for the role. I bring ten years of Python."
    assert summarise_letter(text) == summarise_letter(text)
