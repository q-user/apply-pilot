"""Persistence orchestration for the M3 deep scoring slice (issue #29).

The :class:`ScoringService` glues the LLM scorer to the
:class:`VacancyMatch` repository:

* :meth:`score_match` — score one match and persist the outcome.
* :meth:`score_pending_matches` — drain the pending queue (matches in
  ``new``/``review`` with no score yet) up to a per-call cap.

The service is collaborator-injected: tests wire the
:class:`InMemoryVacancyMatchRepository` fake + an
:class:`InMemoryLLMClient`; production wires the SQLAlchemy-backed
repository + an :class:`HttpLLMClient`. The Protocol-typed repository
means the service compiles against either.

Cross-slice plumbing
--------------------

The in-memory tests need to resolve ``match.vacancy`` and
``match.search_profile`` to build the LLM prompt. The real SQL model
does not carry those attributes by default; in production a
``MatchToPair`` callable resolves them through joins. Tests can either
attach the related objects directly to the in-memory row or pass a
``match_to_pair`` callable to :class:`ScoringService`.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Protocol, runtime_checkable

from job_apply.features.matches.models import VacancyMatch
from job_apply.features.matches.repository import VacancyMatchRepository
from job_apply.features.scoring.llm import LLMScorer
from job_apply.features.search_profiles.models import SearchProfile
from job_apply.features.sources.models import Vacancy

#: Callable that resolves a :class:`VacancyMatch` to its inputs. The
#: default resolver (used by the in-memory tests) reads the related
#: objects off the match itself; the production resolver performs the
#: SQL joins.
MatchToPair = Callable[[VacancyMatch], tuple[Vacancy, SearchProfile]]


def _default_match_to_pair(match: VacancyMatch) -> tuple[Vacancy, SearchProfile]:
    """Resolve a match to its ``(vacancy, profile)`` pair via attributes.

    The SQLAlchemy :class:`VacancyMatch` model does not declare a
    relationship to :class:`Vacancy` or :class:`SearchProfile`, so this
    default reads whatever the caller (or test) attached. Production
    wiring passes a join-based resolver via ``match_to_pair=`` to
    :class:`ScoringService`.
    """
    vacancy = match.vacancy  # type: ignore[attr-defined]
    profile = match.search_profile  # type: ignore[attr-defined]
    if vacancy is None or profile is None:
        raise RuntimeError(
            f"cannot resolve vacancy/profile for match {match.id}; "
            "pass a join-based `match_to_pair` callable to ScoringService."
        )
    return vacancy, profile


@runtime_checkable
class _HasMatchAttrs(Protocol):
    """Duck-typed Protocol the service reads off the match.

    The SQLAlchemy model does not declare a relationship; tests attach
    :attr:`vacancy` and :attr:`search_profile` directly on the row.
    Defining a Protocol here keeps the service compileable under
    static type checkers without changing the production model.
    """

    vacancy: Vacancy | None
    search_profile: SearchProfile | None


class ScoringService:
    """Orchestrate the LLM scoring flow against the matches repository."""

    __slots__ = ("_match_repo", "_match_to_pair", "_scorer")

    def __init__(
        self,
        *,
        scorer: LLMScorer,
        match_repo: VacancyMatchRepository,
        match_to_pair: MatchToPair | None = None,
    ) -> None:
        self._scorer = scorer
        self._match_repo = match_repo
        self._match_to_pair: MatchToPair = match_to_pair or _default_match_to_pair

    @property
    def scorer(self) -> LLMScorer:
        """Return the injected scorer (read-only)."""
        return self._scorer

    @property
    def match_repo(self) -> VacancyMatchRepository:
        """Return the injected match repository (read-only)."""
        return self._match_repo

    async def score_match(
        self,
        match: VacancyMatch,
        *,
        resume_text: str | None = None,
    ) -> VacancyMatch:
        """Score ``match`` and persist the outcome.

        Resolves the match's ``(vacancy, profile)`` pair, asks the
        :class:`LLMScorer` for a :class:`ScoreResult`, then writes the
        result back via
        :meth:`VacancyMatchRepository.update_scoring`. The repository
        moves the row to :attr:`MatchStatus.SCORED` as part of the
        write so the queue is one query away.
        """
        if match.id is None:
            raise ValueError("VacancyMatch.id must be set before scoring")
        vacancy, profile = self._match_to_pair(match)
        result = await self._scorer.score(vacancy, profile, resume_text=resume_text)
        return self._match_repo.update_scoring(
            match.id,
            score=result.score,
            explanation=result.explanation,
            prompt_version=result.prompt_version,
            confidence=result.confidence,
            scored_at=datetime.now(UTC),
        )

    async def score_pending_matches(
        self,
        *,
        limit: int = 50,
    ) -> int:
        """Score every match in the pending queue, capped at ``limit``.

        Returns the number of matches successfully scored. The method
        is fail-soft: a per-match scoring error is logged via the
        underlying :class:`LLMScorer` exception path and the loop
        continues. Failures bubble up unhandled today; the slice does
        not yet own a retry policy.
        """
        pending = self._match_repo.list_pending(limit=limit)
        scored = 0
        for match in pending:
            await self.score_match(match)
            scored += 1
        return scored


__all__ = ["MatchToPair", "ScoringService"]
