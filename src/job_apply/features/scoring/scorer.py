"""LLM-based deep scorer (issue #29).

The :class:`LLMScorer` is the heart of the scoring vertical slice: it
takes a ``(vacancy, profile)`` pair, looks up the active
:class:`PromptVersion` from the injected registry, builds the prompt
via :func:`build_vacancy_scoring_prompt`, sends it to the LLM, and
parses the response into a :class:`ScoreResult`.

Design choices
--------------

* **DI everywhere**: the LLM client and the prompt registry are
  injected through the constructor. Tests can swap them for fakes;
  production wires the :class:`HttpLLMClient` and a SQL-backed
  registry.
* **No Mock, no monkey-patching**: tests use
  :class:`InMemoryLLMClient` and a static :class:`PromptVersion`
  registry so the behaviour is exercised end-to-end.
* **Resume text is optional**: callers can pass a ``resume_text=``
  argument or inject a :class:`resume_text_provider` callable. The
  provider pattern keeps the scorer decoupled from the resumes slice
  — the slice doesn't import the resumes module, the caller decides
  how to fetch the text.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from job_apply.features.scoring.parsing import ScoreResult, parse_score_response
from job_apply.features.scoring.prompts import build_vacancy_scoring_prompt
from job_apply.features.search_profiles.models import SearchProfile
from job_apply.features.sources.models import Vacancy

#: Re-exported here so the public surface of the slice stays in one
#: place. The canonical definition lives in :mod:`.parsing` to avoid
#: a circular import (the parser builds a ``ScoreResult``).
__all__ = [
    "LLMClient",
    "LLMScorer",
    "PromptVersion",
    "PromptVersionRegistry",
    "ResumeTextProvider",
    "ScoreResult",
]


# ---------------------------------------------------------------------------
# Value objects
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PromptVersion:
    """A versioned prompt template.

    The registry returns the active version for a given prompt name;
    the scorer stamps the ``version`` onto the resulting
    :class:`ScoreResult` so later audits know which template produced
    the score.

    Attributes
    ----------
    name:
        Stable, logical prompt name (``"vacancy_scoring"``).
    version:
        Version label (``"v1"``, ``"2024-01-15"`` — free-form, just
        needs to be unique per ``name``).
    template:
        The template body. Templates are rendered with
        :func:`build_vacancy_scoring_prompt` for the LLM; the
        ``template`` field is here so the registry can be used for
        other prompts in the future, but :class:`LLMScorer` does not
        read it directly.
    """

    name: str
    version: str
    template: str = ""


# ---------------------------------------------------------------------------
# Protocol: the LLM client
# ---------------------------------------------------------------------------


@runtime_checkable
class LLMClient(Protocol):
    """The minimal interface :class:`LLMScorer` depends on.

    The Protocol is intentionally tiny: only :meth:`complete`. The
    settings, transport, and retry logic live in the concrete
    implementations; the scorer doesn't care.
    """

    async def complete(
        self,
        prompt: str,
        *,
        temperature: float = 0.2,
        max_tokens: int = 1024,
    ) -> str: ...


# ---------------------------------------------------------------------------
# Protocol: the prompt registry
# ---------------------------------------------------------------------------


@runtime_checkable
class PromptVersionRegistry(Protocol):
    """The minimal interface :class:`LLMScorer` depends on for prompts.

    The registry owns the active version of each named prompt. The
    scorer only needs :meth:`get`; concrete implementations may add
    :meth:`register`, :meth:`list_versions`, etc.
    """

    def get(self, name: str) -> PromptVersion: ...


# ---------------------------------------------------------------------------
# The scorer
# ---------------------------------------------------------------------------


#: Type alias for a callable that returns the resume text for a given
#: search profile. The callable receives the :class:`SearchProfile`
#: and must return a string (possibly empty).
ResumeTextProvider = Callable[[SearchProfile], str]


class LLMScorer:
    """Score a ``(vacancy, profile)`` pair via the LLM.

    The scorer is the *only* piece of the slice that orchestrates the
    LLM call. The orchestrator (:class:`~.service.ScoringService`)
    stitches the scorer to the matches repository; the prompt registry
    owns the template lifecycle; the LLM client owns the wire format.
    The scorer composes the three.

    Parameters
    ----------
    llm:
        Any object implementing the :class:`LLMClient` Protocol.
    prompt_registry:
        Any object implementing the :class:`PromptVersionRegistry`
        Protocol. The scorer always asks for the ``"vacancy_scoring"``
        prompt; the active version is recorded on the
        :class:`ScoreResult`.
    resume_text_provider:
        Optional callable returning the resume text for a profile.
        Used as a fallback when the caller does not pass ``resume_text``
        directly. Decoupling the resumes slice this way keeps the
        scorer free of cross-slice imports.
    """

    __slots__ = ("_llm", "_prompt_registry", "_resume_text_provider")

    #: The logical name the scorer looks up in the prompt registry.
    #: Kept as a class constant so tests and operators can reference
    #: it without string duplication.
    PROMPT_NAME: str = "vacancy_scoring"

    def __init__(
        self,
        llm: LLMClient,
        prompt_registry: PromptVersionRegistry,
        resume_text_provider: ResumeTextProvider | None = None,
    ) -> None:
        self._llm = llm
        self._prompt_registry = prompt_registry
        self._resume_text_provider = resume_text_provider

    @property
    def llm(self) -> LLMClient:
        """Return the injected LLM client (read-only)."""
        return self._llm

    @property
    def prompt_registry(self) -> PromptVersionRegistry:
        """Return the injected prompt registry (read-only)."""
        return self._prompt_registry

    # -- public API -------------------------------------------------------

    async def score(
        self,
        vacancy: Vacancy,
        profile: SearchProfile,
        *,
        resume_text: str | None = None,
    ) -> ScoreResult:
        """Score ``vacancy`` for ``profile`` via the LLM.

        The pipeline:

        1. Resolve the active :class:`PromptVersion` for the
           ``vacancy_scoring`` prompt.
        2. Optionally fetch a resume text (inline argument wins over
           the provider).
        3. Build the LLM prompt via
           :func:`build_vacancy_scoring_prompt`.
        4. Call the LLM and parse the response into a
           :class:`ScoreResult`.
        5. Stamp the active prompt version onto the result.

        Raises
        ------
        job_apply.features.scoring.parsing.LLMScoreParseError
            When the LLM's response cannot be turned into a
            :class:`ScoreResult`.
        """
        prompt_version = self._prompt_registry.get(self.PROMPT_NAME)
        effective_resume = self._resolve_resume(profile, resume_text)
        prompt = build_vacancy_scoring_prompt(vacancy, profile, resume_text=effective_resume)
        raw = await self._llm.complete(prompt)
        parsed = parse_score_response(raw)
        return ScoreResult(
            score=parsed.score,
            explanation=parsed.explanation,
            prompt_version=prompt_version.version,
            confidence=parsed.confidence,
        )

    # -- internals --------------------------------------------------------

    def _resolve_resume(self, profile: SearchProfile, inline: str | None) -> str | None:
        """Pick the resume text to include in the prompt.

        Precedence:

        1. The explicit ``resume_text`` argument (wins).
        2. The :attr:`resume_text_provider` callable, if configured.
        3. ``None`` — the prompt is rendered without a resume section.
        """
        if inline is not None:
            return inline
        if self._resume_text_provider is None:
            return None
        return self._resume_text_provider(profile)
