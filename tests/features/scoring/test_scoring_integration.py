"""End-to-end test for the LLM scoring pipeline (issue #29).

A single test that wires the full stack together with in-memory
fakes — the scorer, the matches repository, the search-profile
repository, the vacancy repository — and verifies that scoring a
match actually lands the score on the row. The test mirrors what a
background worker would do; no production I/O is touched.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterator

import pytest

from job_apply.features.matches.models import MatchStatus, VacancyMatch
from job_apply.features.matches.repository import InMemoryVacancyMatchRepository
from job_apply.features.scoring.llm import InMemoryLLMClient
from job_apply.features.scoring.scorer import LLMScorer, PromptVersion
from job_apply.features.scoring.service import ScoringService
from job_apply.features.search_profiles.models import SearchProfile
from job_apply.features.search_profiles.repository import InMemorySearchProfileRepository
from job_apply.features.sources.models import Vacancy
from job_apply.features.sources.repository import InMemoryVacancyRepository

# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _StaticPromptRegistry:
    def __init__(self, version: str = "v1") -> None:
        self.version = version

    def get(self, name: str) -> PromptVersion:
        return PromptVersion(name=name, version=self.version, template="x")


@pytest.fixture
def llm_response() -> str:
    return json.dumps({"score": 88, "explanation": "strong match", "confidence": 0.92})


@pytest.fixture
def repositories() -> tuple[
    InMemorySearchProfileRepository,
    InMemoryVacancyRepository,
    InMemoryVacancyMatchRepository,
]:
    sp_repo = InMemorySearchProfileRepository()
    v_repo = InMemoryVacancyRepository()
    m_repo = InMemoryVacancyMatchRepository(
        list_user_profiles=lambda user_id: list(sp_repo.list_by_user(user_id)),
    )
    return sp_repo, v_repo, m_repo


def _seed_match(
    sp_repo: InMemorySearchProfileRepository,
    v_repo: InMemoryVacancyRepository,
    m_repo: InMemoryVacancyMatchRepository,
) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    """Seed a user, a profile, a vacancy, and a new match.

    Returns the (user_id, profile_id, vacancy_id) tuple.
    """
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
        source_id="v-e2e-001",
        title="Senior Python Developer",
        description="Django + FastAPI + Postgres",
        employer_name="Acme Inc",
        salary_from=250_000,
        skills=["Python", "Django", "PostgreSQL"],
        raw_data={},
    )
    v_repo.upsert(vacancy)
    match = VacancyMatch(
        search_profile_id=profile.id,
        vacancy_id=vacancy.id,
        status=MatchStatus.NEW.value,
    )
    m_repo.create(match)
    return user_id, profile.id, vacancy.id


def _build_service(
    m_repo: InMemoryVacancyMatchRepository,
    v_repo: InMemoryVacancyRepository,
    sp_repo: InMemorySearchProfileRepository,
    response: str,
) -> ScoringService:
    """Build a ScoringService with a real matcher using the in-memory repos."""

    def _match_to_pair(match: VacancyMatch) -> tuple[Vacancy, SearchProfile]:
        vacancy = v_repo.get_by_id(match.vacancy_id)
        profile = sp_repo.get_by_id(match.search_profile_id)
        if vacancy is None or profile is None:
            raise RuntimeError(f"missing pair for match {match.id}")
        return vacancy, profile

    client = InMemoryLLMClient(responses={"*": response})
    scorer = LLMScorer(llm=client, prompt_registry=_StaticPromptRegistry(version="v1"))
    return ScoringService(scorer=scorer, match_repo=m_repo, match_to_pair=_match_to_pair)


# ---------------------------------------------------------------------------
# End-to-end
# ---------------------------------------------------------------------------


class TestScoringEndToEnd:
    @pytest.mark.asyncio
    async def test_ingest_then_score(
        self,
        llm_response: str,
        repositories: Iterator,
    ) -> None:
        sp_repo, v_repo, m_repo = repositories
        _, profile_id, vacancy_id = _seed_match(sp_repo, v_repo, m_repo)

        service = _build_service(m_repo, v_repo, sp_repo, llm_response)
        count = await service.score_pending_matches(limit=10)

        assert count == 1

        # The match is now scored with all the expected fields.
        all_matches = list(m_repo._by_id.values())  # noqa: SLF001
        assert len(all_matches) == 1
        match = all_matches[0]

        assert match.score == 88
        assert match.match_reason == "strong match"
        assert match.prompt_version == "v1"
        assert match.confidence == 0.92
        assert match.scored_at is not None
        assert match.status == MatchStatus.SCORED.value
        assert match.search_profile_id == profile_id
        assert match.vacancy_id == vacancy_id

    @pytest.mark.asyncio
    async def test_score_match_returns_updated_match(
        self,
        llm_response: str,
        repositories: Iterator,
    ) -> None:
        sp_repo, v_repo, m_repo = repositories
        _, _, _ = _seed_match(sp_repo, v_repo, m_repo)
        match = next(iter(m_repo._by_id.values()))  # noqa: SLF001

        service = _build_service(m_repo, v_repo, sp_repo, llm_response)
        updated = await service.score_match(match)

        assert updated.id == match.id
        assert updated.score == 88
        assert updated.status == MatchStatus.SCORED.value

    @pytest.mark.asyncio
    async def test_pending_filter_skips_already_scored(
        self,
        llm_response: str,
        repositories: Iterator,
    ) -> None:
        """A match that already has a score is *not* re-scored by
        ``score_pending_matches``."""
        sp_repo, v_repo, m_repo = repositories
        _, _, _ = _seed_match(sp_repo, v_repo, m_repo)
        existing = next(iter(m_repo._by_id.values()))  # noqa: SLF001
        existing.score = 42
        existing.prompt_version = "v0"
        m_repo.update_status(existing.id, MatchStatus.SCORED.value)

        service = _build_service(m_repo, v_repo, sp_repo, llm_response)
        count = await service.score_pending_matches(limit=10)

        assert count == 0
        # The score is still the pre-existing one.
        after = m_repo.get_by_id(existing.id)
        assert after is not None
        assert after.score == 42
        assert after.prompt_version == "v0"

    @pytest.mark.asyncio
    async def test_respects_limit(
        self,
        llm_response: str,
        repositories: Iterator,
    ) -> None:
        """A batch of pending matches is processed up to ``limit``."""
        sp_repo, v_repo, m_repo = repositories
        # Seed three matches (sharing the same profile/vacancy is OK for the test).
        _, _, _ = _seed_match(sp_repo, v_repo, m_repo)
        for _ in range(2):
            match = VacancyMatch(
                search_profile_id=next(iter(sp_repo._by_id.values())).id,  # noqa: SLF001
                vacancy_id=next(iter(v_repo._by_id.values())).id,  # noqa: SLF001
                status=MatchStatus.NEW.value,
            )
            m_repo.create(match)

        service = _build_service(m_repo, v_repo, sp_repo, llm_response)
        count = await service.score_pending_matches(limit=2)

        assert count == 2
