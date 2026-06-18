"""Deterministic, LLM-free letter summariser (M8, issue #66).

The MVP style-memory slice does not need an LLM in the ingestion
pipeline: we ship a tiny, dependency-free heuristic that produces a
short, stable summary for every accepted cover letter. LLM-based
summarisation is a follow-up; the slice can swap the implementation
without touching the persistence or API layers because the service
layer only calls :func:`summarise_letter` and stores whatever it
returns.

Output format
-------------

The summary is a single string with three sections joined by ``;`` so
it can be embedded in a single TEXT column and read back by a simple
``split()`` consumer in the API:

* ``first-sentence: <text>`` — the first sentence of the letter
  (split on ``.``, ``!``, ``?``). Sentence-ending punctuation is
  stripped from the value.
* ``words=<N>`` — the total whitespace-separated token count.
* ``trigrams=<a-b-c, d-e-f, g-h-i>`` — the top-3 trigrams of the
  lower-cased text, each trigram itself joined with ``-`` and the
  selected trigrams joined with ``, `` (comma + space). The trigrams
  section is empty (i.e. ``trigrams=``) when the letter has fewer
  than three words.

Blank or whitespace-only input returns an empty string so the caller
can short-circuit and refuse to write an entry.
"""

from __future__ import annotations

import re
from collections import Counter

# Cap the first-sentence prefix in the summary so a runaway letter
# cannot bloat the per-row ``style_summary`` text column. The detail
# view (``letter_text``) still has the full content; the summary is
# just a quick read-back hint.
_FIRST_SENTENCE_MAX_CHARS = 200

# Cap the number of trigrams in the summary for the same reason.
_TRIGRAMS_IN_SUMMARY = 3


def _split_sentences(text: str) -> list[str]:
    """Split ``text`` on ``.``, ``!``, ``?`` and drop empty fragments.

    The split keeps the punctuation off by stripping the matched
    delimiters. The result preserves the original word order so the
    first non-empty fragment is the "first sentence".
    """
    fragments = re.split(r"[.!?]", text)
    return [fragment.strip() for fragment in fragments if fragment.strip()]


def _top_trigrams(text: str, limit: int) -> list[str]:
    """Return the ``limit`` most common trigrams, joined with ``-``.

    Tokens are lower-cased and stripped of anything that is not a
    letter, digit, or hyphen; consecutive delimiters collapse into a
    single boundary. The function returns ``[]`` when the text has
    fewer than three tokens.
    """
    tokens = re.findall(r"[A-Za-z0-9-]+", text.lower())
    if len(tokens) < 3:
        return []
    counts: Counter[tuple[str, ...]] = Counter()
    for i in range(len(tokens) - 2):
        counts[(tokens[i], tokens[i + 1], tokens[i + 2])] += 1
    return ["-".join(trigram) for trigram, _ in counts.most_common(limit)]


def summarise_letter(text: str) -> str:
    """Return the deterministic style summary for ``text``.

    Returns an empty string when ``text`` is blank so the caller can
    refuse to write an entry. See the module docstring for the output
    format.
    """
    stripped = text.strip()
    if not stripped:
        return ""

    sentences = _split_sentences(stripped)
    first_sentence = sentences[0] if sentences else ""
    first_sentence = first_sentence[:_FIRST_SENTENCE_MAX_CHARS]
    word_count = len(re.findall(r"\S+", stripped))
    trigrams = _top_trigrams(stripped, _TRIGRAMS_IN_SUMMARY)
    trigrams_part = ", ".join(trigrams)

    return f"first-sentence: {first_sentence}; words={word_count}; trigrams={trigrams_part}"


__all__ = ["summarise_letter"]
