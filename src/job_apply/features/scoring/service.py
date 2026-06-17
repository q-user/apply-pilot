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

A/B wiring
----------

When an optional :class:`ScoringExperimentService` is injected, every
call to :meth:`score_match` consults the active experiment for
``experiment_name`` (defaulting to ``"vacancy_scoring"``), assigns a
variant to the match's user, and stamps the produced
:class:`VacancyMatch` with the variant's ``prompt_version`` so the
``prompt_versions`` column on the match reflects what the LLM was
actually called with. The same call also records an outcome row
against the experiment (with ``accepted=False`` — the match has not
yet been accepted at scoring time).

When the experiment service is not injected, the service behaves
exactly as before (the registry's active version wins, no outcomes
are recorded). The "no experiment" path is the default and is the
only one exercised by tests of the existing scoring slice.

Cross-slice plumbing
--------------------

The in-memory tests need to resolve ``match.vacancy`` and
``match.search_profile`` to build the LLM prompt. The real SQL model
does not carry those attributes by default; in production a
``MatchToPair`` callable resolves them through joins. Tests can either
attach the related objects directly to the in-memory row or pass a
``match_to_pair`` callable to :class:`ScoringService`.

The user id is read directly off the search profile the in-memory
test attaches. Production code passes a ``match_to_user_id`` callable
that performs the join.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Protocol, runtime_checkable

from job_apply.features.matches.models import VacancyMatch
from job_apply.features.matches.repository import VacancyMatchRepository
from job_apply.features.scoring.llm import LLMScorer
from job_apply.features.scoring_ab.experiments import ScoringVariant
from job_apply.features.scoring_ab.service import ScoringExperimentService
from job_apply.features.search_profiles.models import SearchProfile
from job_apply.features.sources.models import Vacancy

#: The default experiment name the scoring service looks up. Stays in
#: sync with :data:`~job_apply.features.scoring.llm.LLM_SCORING_PROMPT_NAME`
#: — the active scoring experiment and the active prompt template
#: share the same family name.
DEFAULT_EXPERIMENT_NAME: str = "vacancy_scoring"


#: Callable that resolves a :class:`VacancyMatch` to its inputs. The
#: default resolver (used by the in-memory tests) reads the related
#: objects off the match itself; the production resolver performs the
#: SQL joins.
MatchToPair = Callable[[VacancyMatch], tuple[Vacancy, SearchProfile]]


#: Callable that resolves a :class:`VacancyMatch` to the user id that
#: owns it. The default reads the ``user_id`` off the search profile
#: the in-memory test attaches. Production wires a join-based
#: resolver.
MatchToUserId = Callable[[VacancyMatch], object]


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


def _default_match_to_user_id(match: VacancyMatch) -> object:
    """Resolve a match to the ``user_id`` of its search profile.

    Mirrors :func:`_default_match_to_pair`: the in-memory tests attach
    the search profile to the match; production wires a join-based
    resolver.
    """
    profile = match.search_profile  # type: ignore[attr-defined]
    if profile is None:
        raise RuntimeError(
            f"cannot resolve user_id for match {match.id}; "
            "pass a join-based `match_to_user_id` callable to ScoringService."
        )
    return profile.user_id  # type: ignore[attr-defined]


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

    __slots__ = (
        "_match_repo",
        "_match_to_pair",
        "_match_to_user_id",
        "_scorer",
        "_experiment_service",
        "_experiment_name",
    )

    def __init__(
        self,
        *,
        scorer: LLMScorer,
        match_repo: VacancyMatchRepository,
        match_to_pair: MatchToPair | None = None,
        match_to_user_id: MatchToUserId | None = None,
        experiment_service: ScoringExperimentService | None = None,
        experiment_name: str = DEFAULT_EXPERIMENT_NAME,
    ) -> None:
        self._scorer = scorer
        self._match_repo = match_repo
        self._match_to_pair: MatchToPair = match_to_pair or _default_match_to_pair
        self._match_to_user_id: MatchToUserId = match_to_user_id or _default_match_to_user_id
        self._experiment_service = experiment_service
        self._experiment_name = experiment_name

    @property
    def scorer(self) -> LLMScorer:
        """Return the injected scorer (read-only)."""
        return self._scorer

    @property
    def match_repo(self) -> VacancyMatchRepository:
        """Return the injected match repository (read-only)."""
        return self._match_repo

    @property
    def experiment_service(self) -> ScoringExperimentService | None:
        """Return the injected experiment service (read-only)."""
        return self._experiment_service

    @property
    def experiment_name(self) -> str:
        """Return the experiment name the service consults (read-only)."""
        return self._experiment_name

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

        When an experiment service is wired, the scoring call also
        routes through the A/B bucketing: the user's variant is
        resolved, the LLM call is stamped with the variant's
        ``prompt_version``, and an outcome row is recorded.
        """
        if match.id is None:
            raise ValueError("VacancyMatch.id must be set before scoring")
        vacancy, profile = self._match_to_pair(match)

        # A/B bucketing. When no experiment service is wired, the
        # optional ``prompt_version`` argument stays ``None`` and the
        # scorer falls through to the registry / hardcoded fallback.
        prompt_version: str | None = None
        experiment_id: object | None = None
        variant_name: str | None = None
        if self._experiment_service is not None:
            user_id = self._match_to_user_id(match)
            experiment = self._experiment_service.repo.get_active(self._experiment_name)
            if experiment is not None:
                variant: ScoringVariant | None = self._experiment_service.assign_variant(
                    user_id=user_id,
                    vacancy_id=match.vacancy_id,
                    experiment_name=self._experiment_name,
                )
                if variant is not None:
                    prompt_version = f"{experiment.prompt_name}@{variant.prompt_version}"
                    experiment_id = experiment.id
                    variant_name = variant.name

        result = await self._scorer.score(
            vacancy,
            profile,
            resume_text=resume_text,
            prompt_version=prompt_version,
        )
        updated = self._match_repo.update_scoring(
            match.id,
            score=result.score,
            explanation=result.explanation,
            prompt_version=result.prompt_version,
            confidence=result.confidence,
            scored_at=datetime.now(UTC),
        )

        # Record the outcome (fire-and-forget — the service swallows
        # errors so a misbehaving experiment store never fails the
        # scoring hot path).
        if (
            self._experiment_service is not None
            and experiment_id is not None
            and variant_name is not None
        ):
            self._experiment_service.record_outcome(
                experiment_id=experiment_id,
                variant_name=variant_name,
                user_id=self._match_to_user_id(match),
                vacancy_id=match.vacancy_id,
                score=result.score,
                accepted=False,
            )

        return updated

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


__all__ = [
    "DEFAULT_EXPERIMENT_NAME",
    "MatchToPair",
    "MatchToUserId",
    "ScoringService",
]
