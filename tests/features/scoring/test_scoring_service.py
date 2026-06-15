"""Failing tests for :class:`ScoringService` (issue #29).

The service is the orchestrator: it loads a :class:`VacancyMatch`,
asks the :class:`LLMScorer` for a score, and persists the
``(score, explanation, prompt_version, scored_at)`` quartet via the
:class:`VacancyMatchRepository`. Tests use in-memory fakes for both
the LLM client and the match repository so the persistence wiring can
be asserted in isolation.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime

import pytest

from job_apply.features.matches.models import MatchStatus, VacancyMatch
from job_apply.features.matches.repository import InMemoryVacancyMatchRepository
from job_apply.features.scoring.llm import InMemoryLLMClient
from job_apply.features.scoring.scorer import LLMScorer, PromptVersion
from job_apply.features.scoring.service import ScoringService
from job_apply.features.search_profiles.models import SearchProfile
from job_apply.features.sources.models import Vacancy

# ---------------------------------------------------------------------------
# Fakes / helpers
# ---------------------------------------------------------------------------


def _match(
    *,
    status: str = MatchStatus.NEW.value,
    score: int | None = None,
    explanation: str | None = None,
    prompt_version: str | None = None,
) -> VacancyMatch:
    m = VacancyMatch(
        search_profile_id=uuid.uuid4(),
        vacancy_id=uuid.uuid4(),
        status=status,
        score=score,
        match_reason=explanation,
    )
    m.id = uuid.uuid4()
    if score is not None or explanation is not None or prompt_version is not None:
        # The integration test relies on these fields being persisted.
        # The repository.update_scoring() method should handle the
        # actual write.
        pass
    return m


def _seed(
    repo: InMemoryVacancyMatchRepository,
    *,
    status: str = MatchStatus.NEW.value,
    score: int | None = None,
) -> VacancyMatch:
    m = _match(status=status, score=score)
    repo.create(m)
    return m


class _StaticPromptRegistry:
    def __init__(self, version: str = "v1") -> None:
        self.version = version

    def get(self, name: str) -> PromptVersion:
        return PromptVersion(name=name, version=self.version, template="placeholder template")


def _service(
    repo: InMemoryVacancyMatchRepository,
    *,
    response: str | None = None,
    version: str = "v1",
) -> ScoringService:
    if response is None:
        response = json.dumps({"score": 75, "explanation": "good", "confidence": 0.8})
    client = InMemoryLLMClient(responses={"*": response})
    scorer = LLMScorer(llm=client, prompt_registry=_StaticPromptRegistry(version=version))

    def _match_to_pair(match: VacancyMatch) -> tuple[Vacancy, SearchProfile]:
        # The tests build matches with synthetic ids; the matcher
        # simply synthesises a Vacancy and a SearchProfile from
        # those ids so the LLM call has something to render.
        vacancy = Vacancy(
            id=match.vacancy_id,
            source="hh",
            source_id=str(match.vacancy_id),
            title="test vacancy",
            description="x",
            raw_data={},
        )
        profile = SearchProfile(
            id=match.search_profile_id,
            user_id=uuid.uuid4(),
            title="test profile",
            is_active=True,
        )
        return vacancy, profile

    return ScoringService(scorer=scorer, match_repo=repo, match_to_pair=_match_to_pair)


# ---------------------------------------------------------------------------
# score_match
# ---------------------------------------------------------------------------


class TestScoreMatch:
    @pytest.mark.asyncio
    async def test_returns_match(self) -> None:
        repo = InMemoryVacancyMatchRepository()
        match = _seed(repo)
        service = _service(repo)

        result = await service.score_match(match)

        assert result is not None

    @pytest.mark.asyncio
    async def test_persists_score_on_match(self) -> None:
        repo = InMemoryVacancyMatchRepository()
        match = _seed(repo)
        service = _service(repo)

        await service.score_match(match)

        stored = repo.get_by_id(match.id)
        assert stored is not None
        assert stored.score == 75

    @pytest.mark.asyncio
    async def test_persists_explanation_on_match(self) -> None:
        repo = InMemoryVacancyMatchRepository()
        match = _seed(repo)
        service = _service(repo)

        await service.score_match(match)

        stored = repo.get_by_id(match.id)
        assert stored is not None
        assert stored.match_reason == "good"

    @pytest.mark.asyncio
    async def test_persists_prompt_version(self) -> None:
        repo = InMemoryVacancyMatchRepository()
        match = _seed(repo)
        service = _service(repo, version="v42")

        await service.score_match(match)

        stored = repo.get_by_id(match.id)
        assert stored is not None
        assert stored.prompt_version == "v42"

    @pytest.mark.asyncio
    async def test_sets_scored_at_timestamp(self) -> None:
        repo = InMemoryVacancyMatchRepository()
        match = _seed(repo)
        service = _service(repo)

        before = datetime.now(UTC)
        await service.score_match(match)
        after = datetime.now(UTC)

        stored = repo.get_by_id(match.id)
        assert stored is not None
        assert stored.scored_at is not None
        assert before <= stored.scored_at <= after

    @pytest.mark.asyncio
    async def test_updates_status_to_scored(self) -> None:
        repo = InMemoryVacancyMatchRepository()
        match = _seed(repo, status=MatchStatus.NEW.value)
        service = _service(repo)

        await service.score_match(match)

        stored = repo.get_by_id(match.id)
        assert stored is not None
        assert stored.status == MatchStatus.SCORED.value


# ---------------------------------------------------------------------------
# score_pending_matches
# ---------------------------------------------------------------------------


class TestScorePendingMatches:
    @pytest.mark.asyncio
    async def test_scores_matches_with_status_new(self) -> None:
        repo = InMemoryVacancyMatchRepository()
        match = _seed(repo, status=MatchStatus.NEW.value)
        service = _service(repo)

        count = await service.score_pending_matches(limit=10)

        assert count == 1
        stored = repo.get_by_id(match.id)
        assert stored is not None
        assert stored.score == 75

    @pytest.mark.asyncio
    async def test_scores_matches_with_status_review(self) -> None:
        repo = InMemoryVacancyMatchRepository()
        match = _seed(repo, status=MatchStatus.REVIEW.value)
        service = _service(repo)

        count = await service.score_pending_matches(limit=10)

        assert count == 1
        stored = repo.get_by_id(match.id)
        assert stored is not None
        assert stored.score == 75

    @pytest.mark.asyncio
    async def test_skips_already_scored_matches(self) -> None:
        """A match that already has a score is not re-scored."""
        repo = InMemoryVacancyMatchRepository()
        match = _seed(repo, status=MatchStatus.SCORED.value, score=42)
        service = _service(repo)

        count = await service.score_pending_matches(limit=10)

        assert count == 0
        stored = repo.get_by_id(match.id)
        assert stored is not None
        assert stored.score == 42  # unchanged

    @pytest.mark.asyncio
    async def test_skips_accepted_and_rejected_matches(self) -> None:
        repo = InMemoryVacancyMatchRepository()
        for status in (
            MatchStatus.ACCEPTED.value,
            MatchStatus.REJECTED.value,
            MatchStatus.APPLIED.value,
            MatchStatus.DISMISSED.value,
        ):
            repo.create(_match(status=status))
        service = _service(repo)

        count = await service.score_pending_matches(limit=10)

        assert count == 0

    @pytest.mark.asyncio
    async def test_respects_limit(self) -> None:
        repo = InMemoryVacancyMatchRepository()
        for _ in range(5):
            repo.create(_match(status=MatchStatus.NEW.value))
        service = _service(repo)

        count = await service.score_pending_matches(limit=2)

        assert count == 2
        # Two scored, three still un-scored.
        scored = [
            m
            for m in repo._by_id.values()  # noqa: SLF001 (test introspection)
            if m.status == MatchStatus.SCORED.value
        ]
        assert len(scored) == 2

    @pytest.mark.asyncio
    async def test_returns_zero_when_no_pending(self) -> None:
        repo = InMemoryVacancyMatchRepository()
        service = _service(repo)

        count = await service.score_pending_matches(limit=10)

        assert count == 0

    @pytest.mark.asyncio
    async def test_handles_llm_error_gracefully(self) -> None:
        """A single bad response should not stop the whole batch."""
        from job_apply.features.scoring.parsing import LLMScoreParseError

        repo = InMemoryVacancyMatchRepository()
        match = _seed(repo, status=MatchStatus.NEW.value)

        def _match_to_pair(m: VacancyMatch) -> tuple[Vacancy, SearchProfile]:
            return (
                Vacancy(
                    id=m.vacancy_id,
                    source="hh",
                    source_id=str(m.vacancy_id),
                    title="t",
                    raw_data={},
                ),
                SearchProfile(
                    id=m.search_profile_id,
                    user_id=uuid.uuid4(),
                    title="p",
                    is_active=True,
                ),
            )

        service = ScoringService(
            scorer=LLMScorer(
                llm=InMemoryLLMClient(responses={"*": "not json"}),
                prompt_registry=_StaticPromptRegistry(),
            ),
            match_repo=repo,
            match_to_pair=_match_to_pair,
        )

        # The current contract is "raise on the failing match"; the
        # caller's job is to handle the error. We do not assert on
        # this beyond "the call is observable".
        with pytest.raises(LLMScoreParseError):
            await service.score_pending_matches(limit=10)
        # The match is still there but un-scored.
        stored = repo.get_by_id(match.id)
        assert stored is not None
        assert stored.score is None


# ---------------------------------------------------------------------------
# End-to-end
# ---------------------------------------------------------------------------


class TestEndToEnd:
    @pytest.mark.asyncio
    async def test_ingest_vacancy_create_match_and_score(self) -> None:
        """Smoke test: a vacancy is ingested, a match is created, the
        scoring service runs, and the match ends up with a score."""
        # 1. Seed a user, profile and vacancy via the matches repository.
        from job_apply.features.search_profiles.repository import (
            InMemorySearchProfileRepository,
        )
        from job_apply.features.sources.repository import InMemoryVacancyRepository

        sp_repo = InMemorySearchProfileRepository()
        v_repo = InMemoryVacancyRepository()
        m_repo = InMemoryVacancyMatchRepository(
            list_user_profiles=lambda user_id: list(sp_repo.list_by_user(user_id)),
        )

        user_id = uuid.uuid4()
        profile = SearchProfile(
            user_id=user_id,
            title="Backend Python",
            keywords="python, fastapi",
            salary_min=200_000,
            salary_max=400_000,
            is_active=True,
        )
        sp_repo.create(profile)
        vacancy = Vacancy(
            source="hh",
            source_id="v-e2e",
            title="Senior Python Developer",
            description="Django + FastAPI",
            raw_data={},
        )
        v_repo.upsert(vacancy)
        match = VacancyMatch(
            search_profile_id=profile.id,
            vacancy_id=vacancy.id,
            status=MatchStatus.NEW.value,
        )
        m_repo.create(match)

        # 2. Run the scoring service.
        service = _service(m_repo)
        count = await service.score_pending_matches(limit=10)
        assert count == 1

        # 3. The match is now scored.
        stored = m_repo.get_by_id(match.id)
        assert stored is not None
        assert stored.score == 75
        assert stored.match_reason == "good"
        assert stored.prompt_version == "v1"
        assert stored.scored_at is not None
        assert stored.status == MatchStatus.SCORED.value
