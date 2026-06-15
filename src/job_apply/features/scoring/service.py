"""Orchestrator that wires the LLM scorer to the matches repository.

The :class:`ScoringService` is the public entry point used by callers
— background workers, future FastAPI endpoints, CLI commands — that
need to score a single match or to drain a batch of pending matches.

The service is intentionally thin: it loads a :class:`VacancyMatch`,
asks the injected :class:`~.scorer.LLMScorer` to score it, and
persists the result via the
:class:`~job_apply.features.matches.repository.VacancyMatchRepository`.
All business logic lives in the scorer (LLM interaction) and the
repository (persistence); the service stitches them together.

VSA boundary
------------

The service depends on the *protocol* of the matches repository
(``VacancyMatchRepository``), not on the SQL or in-memory
implementations. This keeps the slice independent: tests inject the
in-memory repo, production wires the SQL repo from the FastAPI
request's session. The same pattern is used in
:mod:`job_apply.features.quick_filter.service`.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Protocol, runtime_checkable

from job_apply.features.matches.models import MatchStatus, VacancyMatch
from job_apply.features.matches.repository import VacancyMatchRepository
from job_apply.features.scoring.scorer import LLMScorer, ScoreResult
from job_apply.features.search_profiles.models import SearchProfile
from job_apply.features.sources.models import Vacancy


@runtime_checkable
class ScoringMatchRepository(VacancyMatchRepository, Protocol):
    """The match-repository surface the scoring service depends on.

    This is just an alias for the existing
    :class:`~job_apply.features.matches.repository.VacancyMatchRepository`
    Protocol. It exists so the service signature documents *which*
    methods are used (``get_by_id``, ``update_scoring``,
    ``update_status``, ``list_pending``); callers can wire either
    the in-memory or the SQL implementation and both satisfy the
    surface.

    The alias is needed because Python's structural typing means the
    service does not need to import it explicitly, but the type
    hint in the constructor still has to be readable.
    """


class ScoringService:
    """Score one or more :class:`VacancyMatch` rows via the LLM.

    The service composes three dependencies:

    * a :class:`LLMScorer` — turns ``(vacancy, profile)`` into a
      :class:`ScoreResult`;
    * a :class:`VacancyMatchRepository` — loads pending matches and
      writes the scoring outcome back;
    * a matcher function — maps a :class:`VacancyMatch` to its
      ``(Vacancy, SearchProfile)`` pair. The matcher is injected so
      the service does not need to know about the matches →
      vacancy/profile join; the FastAPI wiring layer (or the tests)
      decide how to look up the related rows.

    Parameters
    ----------
    scorer:
        The injected :class:`LLMScorer`.
    match_repo:
        The injected :class:`VacancyMatchRepository`.
    match_to_pair:
        Callable returning ``(vacancy, profile)`` for a given match.
        When omitted, :meth:`score_match` raises
        :class:`NotImplementedError`; the matcher is always required
        in production. The omission is intentional: it keeps the
        service's contract honest (a "scoring service" without
        matches is a confusing API).
    """

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
        self._match_to_pair = match_to_pair

    @property
    def scorer(self) -> LLMScorer:
        """Return the injected scorer (read-only)."""
        return self._scorer

    @property
    def match_repo(self) -> VacancyMatchRepository:
        """Return the injected match repository (read-only)."""
        return self._match_repo

    # -- public API -------------------------------------------------------

    async def score_match(self, match: VacancyMatch) -> VacancyMatch:
        """Score a single :class:`VacancyMatch` and persist the result.

        The pipeline:

        1. Resolve the ``(Vacancy, SearchProfile)`` pair from the
           match via the injected matcher.
        2. Call :meth:`LLMScorer.score` to produce a
           :class:`ScoreResult`.
        3. Persist ``(score, explanation, prompt_version,
           confidence, scored_at)`` via
           :meth:`VacancyMatchRepository.update_scoring`.
        4. Flip the match's status to ``"scored"`` via
           :meth:`VacancyMatchRepository.update_status`.

        The two repository calls are kept separate so each can fail
        without leaving a half-scored row in the database; a future
        revision can collapse them into a single transaction if the
        match model grows a "scoring in progress" marker.
        """
        vacancy, profile = self._require_pair(match)
        result = await self._scorer.score(vacancy, profile)
        scored_at = datetime.now(UTC)
        self._match_repo.update_scoring(
            match.id,
            score=result.score,
            explanation=result.explanation,
            prompt_version=result.prompt_version,
            confidence=result.confidence,
            scored_at=scored_at,
        )
        updated = self._match_repo.update_status(match.id, MatchStatus.SCORED.value)
        return updated

    async def score_pending_matches(self, limit: int = 50) -> int:
        """Score every match that has no score yet, up to ``limit``.

        A "pending" match is one whose status is ``"new"`` or
        ``"review"`` *and* whose ``score`` column is still ``NULL``.
        The repository's :meth:`list_pending` applies the filter
        and orders by ``created_at`` so the oldest matches are
        scored first.

        Returns the number of matches that were successfully scored.
        An LLM parse error on a single match propagates as an
        exception; the caller is responsible for deciding whether
        to retry, log, or move on. We do not swallow the error
        because silent partial batches are worse than a loud crash.
        """
        pending = self._match_repo.list_pending(limit=limit)
        scored = 0
        for match in pending:
            await self.score_match(match)
            scored += 1
        return scored

    # -- helpers ---------------------------------------------------------

    def _require_pair(self, match: VacancyMatch) -> tuple[Vacancy, SearchProfile]:
        """Resolve the ``(vacancy, profile)`` pair for ``match``.

        The matcher is injected at construction time; when it is
        ``None`` (only possible in a misconfigured production
        wiring) the service raises so the misconfiguration is loud.
        """
        if self._match_to_pair is None:
            raise RuntimeError(
                "ScoringService.match_to_pair is not configured; "
                "construct the service with `match_to_pair=...` to score matches."
            )
        return self._match_to_pair(match)


#: Type alias for the ``(vacancy, profile)`` matcher. The matcher
#: receives a :class:`VacancyMatch` and returns the pair of ORM
#: objects the scorer needs. Wiring is the orchestrator's job; the
#: scorer itself never invokes the matcher.
MatchToPair = Callable[[VacancyMatch], tuple[Vacancy, SearchProfile]]


# Silence unused-import warnings on the UUID import; the alias
# docstring above references it.
_ = uuid

__all__ = [
    "MatchToPair",
    "ScoringMatchRepository",
    "ScoringService",
    "ScoreResult",
]
