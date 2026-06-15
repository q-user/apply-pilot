"""LLM scoring vertical slice (issue #29).

The slice turns a ``(Vacancy, SearchProfile)`` pair into a
:class:`ScoreResult` — a 0–100 score with a free-text explanation,
the LLM's confidence, and the prompt version that produced the
verdict. The slice has four moving parts:

* :class:`LLMScorer` — the orchestrator of the LLM call. It pulls a
  :class:`PromptVersion` from a registry, builds the prompt, calls the
  LLM, and parses the response.
* :class:`ScoringService` — the orchestrator of the persistence
  side. It loads :class:`~job_apply.features.matches.models.VacancyMatch`
  rows, calls the scorer, and writes ``(score, explanation,
  prompt_version, confidence, scored_at)`` back via the
  :class:`~job_apply.features.matches.repository.VacancyMatchRepository`.
* :class:`InMemoryPromptVersionRegistry` (and
  :func:`seed_default_prompts`) — owns the *active* version of each
  named prompt. Production wires the seed at startup; tests build a
  registry inline.
* :class:`HttpLLMClient` and :class:`InMemoryLLMClient` — the two
  LLM client implementations. The HTTP client targets the
  OpenAI-compatible ``/v1/chat/completions`` endpoint; the in-memory
  one is what the test suite uses.

The response parser (:mod:`.parsing`), the prompt builder
(:mod:`.prompts`), and the value objects (:mod:`.scorer`) are
implementation details; the public contract of the slice is the
:class:`ScoringService` and the types it exposes.

VSA boundary
------------

This slice depends on the *protocols* of the matches repository and
the search-profile / vacancy repositories — not on the SQL or
in-memory implementations. Tests pass in-memory fakes; production
wires the SQL implementations from the FastAPI request's session.
Cross-slice imports are limited to the ORM models and the
:mod:`job_apply.features.matches.repository` Protocol.
"""

from __future__ import annotations

from job_apply.features.scoring.llm import (
    HttpLLMClient,
    InMemoryLLMClient,
    InMemoryResponses,
    LLMClient,
    LLMSettings,
)
from job_apply.features.scoring.parsing import (
    LLMScoreParseError,
    ScoreResult,
    parse_score_response,
)
from job_apply.features.scoring.prompts import build_vacancy_scoring_prompt
from job_apply.features.scoring.registry import (
    VACANCY_SCORING_DEFAULT_TEMPLATE,
    VACANCY_SCORING_DEFAULT_VERSION,
    VACANCY_SCORING_PROMPT_NAME,
    InMemoryPromptVersionRegistry,
    seed_default_prompts,
)
from job_apply.features.scoring.scorer import (
    LLMScorer,
    PromptVersion,
    PromptVersionRegistry,
    ResumeTextProvider,
)
from job_apply.features.scoring.service import (
    MatchToPair,
    ScoringMatchRepository,
    ScoringService,
)

__all__ = [
    "HttpLLMClient",
    "InMemoryLLMClient",
    "InMemoryPromptVersionRegistry",
    "InMemoryResponses",
    "LLMClient",
    "LLMScoreParseError",
    "LLMScorer",
    "LLMSettings",
    "MatchToPair",
    "PromptVersion",
    "PromptVersionRegistry",
    "ResumeTextProvider",
    "ScoreResult",
    "ScoringMatchRepository",
    "ScoringService",
    "VACANCY_SCORING_DEFAULT_TEMPLATE",
    "VACANCY_SCORING_DEFAULT_VERSION",
    "VACANCY_SCORING_PROMPT_NAME",
    "build_vacancy_scoring_prompt",
    "parse_score_response",
    "seed_default_prompts",
]
