"""TDD tests for wiring the A/B experiment into the scoring service (issue #65).

When the :class:`ScoringService` is constructed with an
:class:`ScoringExperimentService`, every call to :meth:`score_match`:

1. resolves the active experiment for the vacancy-scoring family,
2. assigns a variant for the match's user via deterministic bucketing,
3. passes the variant's ``prompt_version`` to the LLM scorer (overriding
   the registry's "active" version),
4. records the outcome against the experiment (with ``accepted=False``
   — the match is not yet accepted at scoring time).

When no experiment is wired, the service behaves exactly as before.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime

import pytest

# Pre-load ``sources.models`` before any ``matches.*`` import.
#
# The ``matches`` package's ``__init__`` triggers an import of
# ``matches.service`` which in turn pulls in ``Vacancy`` from
# ``sources.models``; that chain ends up loading
# ``telegram.actions.accept`` which then re-enters ``matches.service``
# while it is still partially initialised (the ``MatchNotFoundError``
# class has not yet been bound). Pre-loading ``sources.models`` up
# front means the cycle resolves the second time around — once
# ``matches.service`` is fully loaded, subsequent re-imports return
# the cached module from ``sys.modules`` and the cycle is broken.
import job_apply.features.sources.models  # noqa: F401  (pre-load to break circular import)
from job_apply.features.matches.models import MatchStatus, VacancyMatch
from job_apply.features.matches.repository import InMemoryVacancyMatchRepository
from job_apply.features.scoring.llm import (
    WILDCARD_PROMPT,
    InMemoryLLMClient,
    LLMScorer,
)
from job_apply.features.scoring.service import ScoringService
from job_apply.features.scoring_ab.experiments import (
    InMemoryScoringExperimentRepository,
    ScoringExperiment,
    ScoringVariant,
)
from job_apply.features.scoring_ab.service import ScoringExperimentService
from job_apply.features.search_profiles.models import SearchProfile
from job_apply.features.sources.models import Vacancy

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _vacancy(source_id: str = "v-001") -> Vacancy:
    v = Vacancy(
        source="hh",
        source_id=source_id,
        title="Senior Python Developer",
        description="FastAPI + Postgres + AWS",
        url=f"https://hh.ru/vacancy/{source_id}",
        salary_from=200_000,
        salary_to=350_000,
        salary_currency="RUR",
        salary_gross=False,
        employer_name="Acme",
        location="Москва",
        schedule="remote",
        experience="5+ years",
        skills=["Python", "FastAPI", "PostgreSQL"],
        raw_data={"id": source_id, "name": "Senior Python Developer"},
    )
    v.id = uuid.uuid4()
    return v


def _profile(user_id: uuid.UUID) -> SearchProfile:
    p = SearchProfile(
        user_id=user_id,
        title="Backend Python",
        keywords="python, fastapi, postgres",
        salary_min=250_000,
        salary_max=400_000,
        location="Москва / remote",
        schedule="remote",
        is_active=True,
    )
    p.id = uuid.uuid4()
    return p


def _variant(name: str, prompt_version: str, weight: float) -> ScoringVariant:
    return ScoringVariant(name=name, prompt_version=prompt_version, weight=weight)


def _experiment() -> ScoringExperiment:
    return ScoringExperiment(
        id=uuid.uuid4(),
        name="vacancy_scoring",
        prompt_name="vacancy_scoring",
        variants=[
            _variant("control", "1.0.0", 0.5),
            _variant("treatment", "1.1.0", 0.5),
        ],
        active=True,
        created_at=datetime(2026, 6, 17, 12, 0, 0, tzinfo=UTC),
    )


@pytest.fixture
def user_id() -> uuid.UUID:
    return uuid.uuid4()


@pytest.fixture
def profile(user_id: uuid.UUID) -> SearchProfile:
    return _profile(user_id)


@pytest.fixture
def vacancy() -> Vacancy:
    return _vacancy()


@pytest.fixture
def match_repo() -> InMemoryVacancyMatchRepository:
    return InMemoryVacancyMatchRepository()


@pytest.fixture
def experiment_repo() -> InMemoryScoringExperimentRepository:
    return InMemoryScoringExperimentRepository()


@pytest.fixture
def experiment_service(
    experiment_repo: InMemoryScoringExperimentRepository,
) -> ScoringExperimentService:
    return ScoringExperimentService(experiment_repo)


@pytest.fixture
def llm_client() -> InMemoryLLMClient:
    return InMemoryLLMClient(
        responses={
            WILDCARD_PROMPT: json.dumps({"score": 72, "explanation": "fine", "confidence": 0.7})
        }
    )


@pytest.fixture
def scorer(llm_client: InMemoryLLMClient) -> LLMScorer:
    return LLMScorer(llm=llm_client)


# ---------------------------------------------------------------------------
# Wiring — the experiment service overrides the prompt_version
# ---------------------------------------------------------------------------


def _build_match(profile: SearchProfile, vacancy: Vacancy) -> VacancyMatch:
    match = VacancyMatch(
        search_profile_id=profile.id,
        vacancy_id=vacancy.id,
        status=MatchStatus.NEW.value,
    )
    match.id = uuid.uuid4()
    match.created_at = datetime.now(UTC)
    match.search_profile = profile  # type: ignore[attr-defined]
    match.vacancy = vacancy  # type: ignore[attr-defined]
    return match


@pytest.mark.asyncio
async def test_score_match_uses_variant_prompt_version(
    scorer: LLMScorer,
    match_repo: InMemoryVacancyMatchRepository,
    experiment_service: ScoringExperimentService,
    experiment_repo: InMemoryScoringExperimentRepository,
    profile: SearchProfile,
    vacancy: Vacancy,
) -> None:
    """When an experiment is wired, the variant's prompt_version wins."""
    experiment = _experiment()
    experiment_repo.add(experiment)
    match = _build_match(profile, vacancy)
    match_repo.create(match)

    service = ScoringService(
        scorer=scorer,
        match_repo=match_repo,
        experiment_service=experiment_service,
        experiment_name="vacancy_scoring",
    )

    updated = await service.score_match(match)

    variant = experiment_service.assign_variant(
        user_id=profile.user_id, vacancy_id=vacancy.id, experiment_name="vacancy_scoring"
    )
    assert variant is not None
    assert updated.prompt_version == f"vacancy_scoring@{variant.prompt_version}"


@pytest.mark.asyncio
async def test_score_match_records_outcome(
    scorer: LLMScorer,
    match_repo: InMemoryVacancyMatchRepository,
    experiment_service: ScoringExperimentService,
    experiment_repo: InMemoryScoringExperimentRepository,
    profile: SearchProfile,
    vacancy: Vacancy,
) -> None:
    """A scoring run records an outcome row in the experiment repository."""
    experiment = _experiment()
    experiment_repo.add(experiment)
    match = _build_match(profile, vacancy)
    match_repo.create(match)

    service = ScoringService(
        scorer=scorer,
        match_repo=match_repo,
        experiment_service=experiment_service,
        experiment_name="vacancy_scoring",
    )

    await service.score_match(match)

    outcomes = experiment_repo.list_outcomes(experiment.id)
    assert len(outcomes) == 1
    outcome = outcomes[0]
    variant = experiment_service.assign_variant(
        user_id=profile.user_id, vacancy_id=vacancy.id, experiment_name="vacancy_scoring"
    )
    assert outcome["variant_name"] == variant.name
    assert outcome["user_id"] == profile.user_id
    assert outcome["vacancy_id"] == vacancy.id
    assert outcome["score"] == 72
    # Scoring is "not yet accepted" — ``accepted`` defaults to ``False``.
    assert outcome["accepted"] is False


@pytest.mark.asyncio
async def test_score_match_without_experiment_service_uses_baseline(
    scorer: LLMScorer,
    match_repo: InMemoryVacancyMatchRepository,
    profile: SearchProfile,
    vacancy: Vacancy,
) -> None:
    """No experiment service → the hardcoded ``vacancy_scoring@1.0.0`` is used."""
    match = _build_match(profile, vacancy)
    match_repo.create(match)

    service = ScoringService(scorer=scorer, match_repo=match_repo)

    updated = await service.score_match(match)

    assert updated.prompt_version == "vacancy_scoring@1.0.0"


@pytest.mark.asyncio
async def test_score_match_with_inactive_experiment_uses_baseline(
    scorer: LLMScorer,
    match_repo: InMemoryVacancyMatchRepository,
    experiment_service: ScoringExperimentService,
    experiment_repo: InMemoryScoringExperimentRepository,
    profile: SearchProfile,
    vacancy: Vacancy,
) -> None:
    """An inactive experiment must not change the prompt_version stamp."""
    experiment = _experiment()
    object.__setattr__(experiment, "active", False)
    experiment_repo.add(experiment)
    match = _build_match(profile, vacancy)
    match_repo.create(match)

    service = ScoringService(
        scorer=scorer,
        match_repo=match_repo,
        experiment_service=experiment_service,
        experiment_name="vacancy_scoring",
    )

    updated = await service.score_match(match)

    assert updated.prompt_version == "vacancy_scoring@1.0.0"
    # No outcomes recorded when the experiment is inactive.
    assert experiment_repo.list_outcomes(experiment.id) == []


@pytest.mark.asyncio
async def test_score_match_deterministic_for_same_user(
    scorer: LLMScorer,
    match_repo: InMemoryVacancyMatchRepository,
    experiment_service: ScoringExperimentService,
    experiment_repo: InMemoryScoringExperimentRepository,
    user_id: uuid.UUID,
) -> None:
    """The same user always lands in the same variant across multiple matches."""
    experiment = _experiment()
    experiment_repo.add(experiment)
    profile = _profile(user_id)

    service = ScoringService(
        scorer=scorer,
        match_repo=match_repo,
        experiment_service=experiment_service,
        experiment_name="vacancy_scoring",
    )

    prompt_versions: set[str] = set()
    for i in range(3):
        vacancy = _vacancy(source_id=f"v-{i}")
        match = _build_match(profile, vacancy)
        match_repo.create(match)
        updated = await service.score_match(match)
        prompt_versions.add(updated.prompt_version)

    # The user is bucketed once; every match should use the same prompt version.
    assert len(prompt_versions) == 1
