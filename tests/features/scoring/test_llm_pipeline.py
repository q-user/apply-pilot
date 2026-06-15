"""TDD tests for the M3 deep LLM scoring pipeline (issue #29).

Covers the entire ``features/scoring`` vertical slice:

* :class:`InMemoryLLMClient` + :class:`HttpLLMClient` (httpx.MockTransport)
* :func:`build_vacancy_scoring_prompt` prompt builder
* :func:`parse_score_response` tolerant JSON parser
* :class:`LLMScorer` orchestrator
* :class:`ScoringService` end-to-end persistence flow

All HTTP traffic is faked via :class:`httpx.MockTransport`; no real
network call is ever made. Match persistence uses the in-memory
``VacancyMatchRepository`` fake — no SQL, no ``Mock``.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime

import httpx
import pytest

from job_apply.features.matches.models import MatchStatus, VacancyMatch
from job_apply.features.matches.repository import InMemoryVacancyMatchRepository
from job_apply.features.scoring.llm import (
    WILDCARD_PROMPT,
    HttpLLMClient,
    InMemoryLLMClient,
    LLMScoreParseError,
    LLMScorer,
    LLMSettings,
    ScoreResult,
    parse_score_response,
)
from job_apply.features.scoring.prompts import build_vacancy_scoring_prompt
from job_apply.features.scoring.service import ScoringService
from job_apply.features.search_profiles.models import SearchProfile
from job_apply.features.sources.models import Vacancy

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _vacancy(
    *,
    source_id: str = "v-001",
    title: str = "Senior Python Developer",
    description: str = "FastAPI + Postgres + AWS",
    employer_name: str | None = "Acme",
    location: str | None = "Москва",
    salary_from: int | None = 200_000,
    salary_to: int | None = 350_000,
    schedule: str | None = "remote",
    experience: str | None = "5+ years",
    skills: list[str] | None = None,
) -> Vacancy:
    """Build a fully-populated :class:`Vacancy` for prompt-builder tests."""
    v = Vacancy(
        source="hh",
        source_id=source_id,
        title=title,
        description=description,
        url="https://hh.ru/vacancy/1",
        salary_from=salary_from,
        salary_to=salary_to,
        salary_currency="RUR",
        salary_gross=False,
        employer_name=employer_name,
        location=location,
        schedule=schedule,
        experience=experience,
        skills=skills if skills is not None else ["Python", "FastAPI", "PostgreSQL"],
        raw_data={"id": source_id, "name": title},
    )
    v.id = uuid.uuid4()
    return v


def _profile(
    user_id: uuid.UUID,
    *,
    title: str = "Backend Python",
    keywords: str = "python, fastapi, postgres",
    salary_min: int | None = 250_000,
    salary_max: int | None = 400_000,
    location: str | None = "Москва / remote",
    schedule: str | None = "remote",
) -> SearchProfile:
    """Build a fully-populated :class:`SearchProfile` for tests."""
    p = SearchProfile(
        user_id=user_id,
        title=title,
        keywords=keywords,
        salary_min=salary_min,
        salary_max=salary_max,
        location=location,
        schedule=schedule,
        is_active=True,
    )
    p.id = uuid.uuid4()
    return p


@pytest.fixture
def user_id() -> uuid.UUID:
    return uuid.uuid4()


@pytest.fixture
def vacancy() -> Vacancy:
    return _vacancy()


@pytest.fixture
def profile(user_id: uuid.UUID) -> SearchProfile:
    return _profile(user_id)


@pytest.fixture
def match_repo() -> InMemoryVacancyMatchRepository:
    return InMemoryVacancyMatchRepository()


# ---------------------------------------------------------------------------
# InMemoryLLMClient
# ---------------------------------------------------------------------------


class TestInMemoryLLMClient:
    @pytest.mark.asyncio
    async def test_returns_loaded_response_for_matching_prompt(self) -> None:
        """A pre-loaded prompt -> response mapping is returned verbatim."""
        client = InMemoryLLMClient(responses={"hello": "world"})

        assert await client.complete("hello") == "world"

    @pytest.mark.asyncio
    async def test_callable_value_is_invoked_with_prompt(self) -> None:
        """A callable value lets tests assert the exact prompt that was sent."""
        captured: list[str] = []

        def responder(prompt: str) -> str:
            captured.append(prompt)
            return "ok"

        client = InMemoryLLMClient(responses={"ping": responder})

        assert await client.complete("ping") == "ok"
        assert captured == ["ping"]

    @pytest.mark.asyncio
    async def test_wildcard_key_matches_any_prompt(self) -> None:
        """The ``"*"`` key returns the same response regardless of prompt."""
        client = InMemoryLLMClient(responses={WILDCARD_PROMPT: "ok"})

        assert await client.complete("anything") == "ok"
        assert await client.complete("something else") == "ok"

    @pytest.mark.asyncio
    async def test_unknown_prompt_raises(self) -> None:
        """Asking for a prompt that wasn't pre-loaded is a loud failure."""
        client = InMemoryLLMClient(responses={"a": "1"})

        with pytest.raises(KeyError):
            await client.complete("missing")


# ---------------------------------------------------------------------------
# HttpLLMClient (httpx.MockTransport)
# ---------------------------------------------------------------------------


class TestHttpLLMClient:
    @pytest.mark.asyncio
    async def test_serializes_openai_chat_completion_request(self) -> None:
        """The request must hit ``/v1/chat/completions`` with the expected body."""
        captured: dict = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["url"] = str(request.url)
            captured["method"] = request.method
            captured["headers"] = dict(request.headers)
            captured["body"] = json.loads(request.content)
            return httpx.Response(
                200,
                json={
                    "choices": [
                        {"message": {"role": "assistant", "content": "hi"}},
                    ]
                },
            )

        settings = LLMSettings(
            api_key="test-key",
            base_url="https://api.example.com/v1",
            model="gpt-4o-mini",
        )
        client = HttpLLMClient(settings, transport=httpx.MockTransport(handler))

        result = await client.complete("ping", temperature=0.1, max_tokens=64)

        assert result == "hi"
        assert captured["method"] == "POST"
        assert captured["url"] == "https://api.example.com/v1/chat/completions"
        assert captured["headers"]["authorization"] == "Bearer test-key"
        body = captured["body"]
        assert body["model"] == "gpt-4o-mini"
        assert body["temperature"] == 0.1
        assert body["max_tokens"] == 64
        # Single user-role message carrying the prompt.
        assert body["messages"] == [{"role": "user", "content": "ping"}]

    @pytest.mark.asyncio
    async def test_propagates_http_error(self) -> None:
        """A 5xx response must surface as :class:`httpx.HTTPStatusError`."""

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(500, json={"error": "boom"})

        settings = LLMSettings(api_key="k", base_url="https://api.example.com/v1", model="m")
        client = HttpLLMClient(settings, transport=httpx.MockTransport(handler))

        with pytest.raises(httpx.HTTPStatusError):
            await client.complete("ping")

    @pytest.mark.asyncio
    async def test_raises_when_response_has_no_choices(self) -> None:
        """A 200 OK with an empty ``choices`` list is a protocol violation."""

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"choices": []})

        settings = LLMSettings(api_key="k", base_url="https://api.example.com/v1", model="m")
        client = HttpLLMClient(settings, transport=httpx.MockTransport(handler))

        with pytest.raises(RuntimeError, match="choices"):
            await client.complete("ping")


# ---------------------------------------------------------------------------
# Prompt builder
# ---------------------------------------------------------------------------


class TestBuildVacancyScoringPrompt:
    def test_includes_all_vacancy_fields(self, vacancy: Vacancy, profile: SearchProfile) -> None:
        prompt = build_vacancy_scoring_prompt(vacancy, profile)

        assert vacancy.title in prompt
        assert vacancy.description in prompt
        assert vacancy.employer_name in prompt
        assert vacancy.location in prompt
        assert "200000" in prompt
        assert "350000" in prompt
        assert vacancy.schedule in prompt
        assert vacancy.experience in prompt
        for skill in vacancy.skills or []:
            assert skill in prompt

    def test_includes_all_profile_fields(self, vacancy: Vacancy, profile: SearchProfile) -> None:
        prompt = build_vacancy_scoring_prompt(vacancy, profile)

        assert profile.title in prompt
        assert profile.keywords in prompt
        assert "250000" in prompt
        assert "400000" in prompt
        assert profile.location in prompt
        assert profile.schedule in prompt

    def test_includes_resume_section_when_provided(
        self, vacancy: Vacancy, profile: SearchProfile
    ) -> None:
        resume = "10 years of Python. Last 5 years on FastAPI + Postgres + AWS."

        prompt = build_vacancy_scoring_prompt(vacancy, profile, resume_text=resume)

        assert "Resume" in prompt
        assert resume in prompt

    def test_omits_resume_section_when_not_provided(
        self, vacancy: Vacancy, profile: SearchProfile
    ) -> None:
        prompt = build_vacancy_scoring_prompt(vacancy, profile, resume_text=None)

        assert "Resume" not in prompt

    def test_instructs_model_to_respond_with_json(
        self, vacancy: Vacancy, profile: SearchProfile
    ) -> None:
        prompt = build_vacancy_scoring_prompt(vacancy, profile)

        assert "JSON" in prompt
        assert "score" in prompt
        assert "explanation" in prompt


# ---------------------------------------------------------------------------
# Response parser
# ---------------------------------------------------------------------------


class TestParseScoreResponse:
    def test_handles_plain_json(self) -> None:
        result = parse_score_response(
            json.dumps({"score": 75, "explanation": "good fit", "confidence": 0.85})
        )

        assert result.score == 75
        assert result.explanation == "good fit"
        assert result.confidence == 0.85

    def test_handles_fenced_json(self) -> None:
        result = parse_score_response(
            "```json\n" + json.dumps({"score": 80, "explanation": "strong"}) + "\n```"
        )

        assert result.score == 80
        assert result.explanation == "strong"

    def test_handles_trailing_comma(self) -> None:
        result = parse_score_response('{"score": 60, "explanation": "ok", "confidence": 0.5,}')

        assert result.score == 60
        assert result.explanation == "ok"
        assert result.confidence == 0.5

    def test_clamps_out_of_range_score_high(self) -> None:
        result = parse_score_response(json.dumps({"score": 250, "explanation": "lofty"}))

        assert result.score == 100

    def test_clamps_out_of_range_score_low(self) -> None:
        result = parse_score_response(json.dumps({"score": -5, "explanation": "low"}))

        assert result.score == 0

    def test_clamps_out_of_range_confidence(self) -> None:
        result = parse_score_response(
            json.dumps({"score": 50, "explanation": "x", "confidence": 1.7})
        )

        assert result.confidence == 1.0

    def test_raises_on_invalid_json(self) -> None:
        with pytest.raises(LLMScoreParseError):
            parse_score_response("not json at all")

    def test_raises_on_missing_score(self) -> None:
        with pytest.raises(LLMScoreParseError):
            parse_score_response(json.dumps({"explanation": "no score"}))

    def test_defaults_missing_optional_fields(self) -> None:
        result = parse_score_response(json.dumps({"score": 42}))

        assert result.score == 42
        assert result.explanation == ""
        assert result.confidence == 1.0
        assert result.prompt_version == ""


# ---------------------------------------------------------------------------
# LLMScorer
# ---------------------------------------------------------------------------


class TestLLMScorer:
    @pytest.mark.asyncio
    async def test_returns_score_result(self, vacancy: Vacancy, profile: SearchProfile) -> None:
        client = InMemoryLLMClient(
            responses={
                WILDCARD_PROMPT: json.dumps(
                    {"score": 88, "explanation": "great", "confidence": 0.9}
                )
            }
        )
        scorer = LLMScorer(llm=client)

        result = await scorer.score(vacancy, profile, resume_text=None)

        assert isinstance(result, ScoreResult)
        assert result.score == 88
        assert result.explanation == "great"
        assert result.confidence == 0.9
        assert result.prompt_version == "vacancy_scoring@1.0.0"

    @pytest.mark.asyncio
    async def test_includes_resume_when_provided(
        self, vacancy: Vacancy, profile: SearchProfile
    ) -> None:
        seen_prompts: list[str] = []

        def responder(prompt: str) -> str:
            seen_prompts.append(prompt)
            return json.dumps({"score": 50, "explanation": "ok"})

        client = InMemoryLLMClient(responses={WILDCARD_PROMPT: responder})
        scorer = LLMScorer(llm=client)

        await scorer.score(vacancy, profile, resume_text="ResumeText123")

        assert any("ResumeText123" in p for p in seen_prompts)


# ---------------------------------------------------------------------------
# ScoringService
# ---------------------------------------------------------------------------


class TestScoringService:
    @pytest.mark.asyncio
    async def test_persists_score_on_match(
        self,
        match_repo: InMemoryVacancyMatchRepository,
        profile: SearchProfile,
        vacancy: Vacancy,
    ) -> None:
        match = VacancyMatch(
            search_profile_id=profile.id,
            vacancy_id=vacancy.id,
            status=MatchStatus.NEW.value,
        )
        match.id = uuid.uuid4()
        match.created_at = datetime.now(UTC)
        match_repo.create(match)
        # The SQL repository resolves vacancy/profile via joins; the in-memory
        # test double uses attribute attachment so the service can read them.
        match.search_profile = profile  # type: ignore[attr-defined]
        match.vacancy = vacancy  # type: ignore[attr-defined]

        client = InMemoryLLMClient(
            responses={
                WILDCARD_PROMPT: json.dumps({"score": 72, "explanation": "fine", "confidence": 0.7})
            }
        )
        scorer = LLMScorer(llm=client)
        service = ScoringService(scorer=scorer, match_repo=match_repo)

        updated = await service.score_match(match)

        assert updated.score == 72
        assert updated.explanation == "fine"
        assert updated.prompt_version == "vacancy_scoring@1.0.0"
        assert updated.scored_at is not None
        # The row in the repository carries the same state.
        stored = match_repo.get_by_id(match.id)
        assert stored is not None
        assert stored.score == 72
        assert stored.explanation == "fine"
        assert stored.scored_at is not None

    @pytest.mark.asyncio
    async def test_score_pending_matches_filters_unscored(
        self,
        match_repo: InMemoryVacancyMatchRepository,
        profile: SearchProfile,
    ) -> None:
        v1 = _vacancy(source_id="v-1")
        v2 = _vacancy(source_id="v-2")
        v3 = _vacancy(source_id="v-3")
        v4 = _vacancy(source_id="v-4")

        new_match = VacancyMatch(
            search_profile_id=profile.id, vacancy_id=v1.id, status=MatchStatus.NEW.value
        )
        new_match.id = uuid.uuid4()
        new_match.search_profile = profile  # type: ignore[attr-defined]
        new_match.vacancy = v1  # type: ignore[attr-defined]

        review_match = VacancyMatch(
            search_profile_id=profile.id,
            vacancy_id=v2.id,
            status=MatchStatus.REVIEW.value,
        )
        review_match.id = uuid.uuid4()
        review_match.search_profile = profile  # type: ignore[attr-defined]
        review_match.vacancy = v2  # type: ignore[attr-defined]

        already_scored = VacancyMatch(
            search_profile_id=profile.id,
            vacancy_id=v3.id,
            status=MatchStatus.SCORED.value,
            score=99,
        )
        already_scored.id = uuid.uuid4()

        accepted_match = VacancyMatch(
            search_profile_id=profile.id,
            vacancy_id=v4.id,
            status=MatchStatus.ACCEPTED.value,
        )
        accepted_match.id = uuid.uuid4()

        for m in (new_match, review_match, already_scored, accepted_match):
            match_repo.create(m)

        client = InMemoryLLMClient(
            responses={
                WILDCARD_PROMPT: json.dumps({"score": 50, "explanation": "ok"}),
            }
        )
        scorer = LLMScorer(llm=client)
        service = ScoringService(scorer=scorer, match_repo=match_repo)

        scored = await service.score_pending_matches()

        # Only the two pending matches (new + review, score IS NULL) are scored.
        assert scored == 2
        assert new_match.score == 50
        assert review_match.score == 50
        # Already-scored / accepted matches are untouched.
        assert already_scored.score == 99
        assert accepted_match.score is None
        # Pending matches move to "scored" status.
        assert new_match.status == MatchStatus.SCORED.value
        assert review_match.status == MatchStatus.SCORED.value
