"""Failing tests for :class:`LLMScorer` (issue #29).

The scorer is the heart of the vertical slice: it pulls a prompt from
the registry, asks the LLM to score a ``(vacancy, profile)`` pair, and
turns the response into a :class:`ScoreResult`. The tests use the
:class:`InMemoryLLMClient` so no real network call is ever made and
the response is fully under the test's control.
"""

from __future__ import annotations

import json
import uuid

import pytest

from job_apply.features.scoring.llm import InMemoryLLMClient
from job_apply.features.scoring.prompts import build_vacancy_scoring_prompt
from job_apply.features.scoring.scorer import (
    LLMScorer,
    PromptVersion,
    ScoreResult,
)
from job_apply.features.search_profiles.models import SearchProfile
from job_apply.features.sources.models import Vacancy

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _vacancy(**overrides) -> Vacancy:
    """Build a Vacancy with sensible defaults; per-field overrides allowed."""
    fields: dict = {
        "source": "hh",
        "source_id": str(uuid.uuid4()),
        "title": "Senior Python Developer",
        "description": "Django, FastAPI, PostgreSQL",
        "employer_name": "Acme Inc",
        "location": "Москва",
        "salary_from": 250_000,
        "salary_to": 350_000,
        "schedule": "fullDay",
        "experience": "3-6 years",
        "skills": ["Python", "Django", "PostgreSQL"],
    }
    fields.update(overrides)
    v = Vacancy(**fields)
    v.id = uuid.uuid4()
    return v


def _profile(**overrides) -> SearchProfile:
    fields: dict = {
        "user_id": uuid.uuid4(),
        "title": "Backend Python",
        "keywords": "python, fastapi, postgres",
        "salary_min": 200_000,
        "salary_max": 400_000,
        "location": "Москва",
        "schedule": "fullDay",
        "is_active": True,
    }
    fields.update(overrides)
    p = SearchProfile(**fields)
    p.id = uuid.uuid4()
    return p


class _StaticPromptRegistry:
    """Minimal in-memory prompt registry the scorer depends on.

    Returns the same template/version every time. Tests that need to
    verify multiple versions or missing prompts can build a more
    sophisticated fake.
    """

    def __init__(self, key: str = "vacancy_scoring", version: str = "v1") -> None:
        self._key = key
        self._version = version
        self.calls: list[str] = []

    def get(self, name: str) -> PromptVersion:
        self.calls.append(name)
        return PromptVersion(
            name=name,
            version=self._version,
            template="SCORE {vacancy_title} for {profile_title}",
        )


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


class TestScorerHappyPath:
    @pytest.mark.asyncio
    async def test_returns_score_result(self) -> None:
        response = json.dumps(
            {
                "score": 88,
                "explanation": "Strong match.",
                "confidence": 0.85,
            }
        )
        client = InMemoryLLMClient(responses={"*": response})
        registry = _StaticPromptRegistry()
        scorer = LLMScorer(llm=client, prompt_registry=registry)

        result = await scorer.score(_vacancy(), _profile())

        assert isinstance(result, ScoreResult)
        assert result.score == 88
        assert result.explanation == "Strong match."
        assert result.confidence == 0.85

    @pytest.mark.asyncio
    async def test_records_prompt_version_on_result(self) -> None:
        response = json.dumps({"score": 50, "explanation": "ok"})
        client = InMemoryLLMClient(responses={"*": response})
        registry = _StaticPromptRegistry(version="v2")
        scorer = LLMScorer(llm=client, prompt_registry=registry)

        result = await scorer.score(_vacancy(), _profile())

        assert result.prompt_version == "v2"

    @pytest.mark.asyncio
    async def test_calls_registry_with_vacancy_scoring(self) -> None:
        response = json.dumps({"score": 50, "explanation": "ok"})
        client = InMemoryLLMClient(responses={"*": response})
        registry = _StaticPromptRegistry()
        scorer = LLMScorer(llm=client, prompt_registry=registry)

        await scorer.score(_vacancy(), _profile())

        assert registry.calls == ["vacancy_scoring"]

    @pytest.mark.asyncio
    async def test_prompt_sent_to_llm_includes_vacancy_and_profile(self) -> None:
        """The actual prompt handed to the LLM must carry the vacancy
        title and profile title so a regression that drops the field is
        caught."""
        seen: list[str] = []

        def fake(prompt: str, *, temperature: float = 0.2, max_tokens: int = 1024) -> str:
            seen.append(prompt)
            return json.dumps({"score": 50, "explanation": "ok"})

        client = InMemoryLLMClient(responses=fake)
        registry = _StaticPromptRegistry()
        scorer = LLMScorer(llm=client, prompt_registry=registry)

        vacancy = _vacancy(title="Staff Backend Engineer")
        profile = _profile(title="Backend Python")
        await scorer.score(vacancy, profile)

        assert "Staff Backend Engineer" in seen[0]
        assert "Backend Python" in seen[0]


# ---------------------------------------------------------------------------
# Resume text injection
# ---------------------------------------------------------------------------


class TestScorerResumeInjection:
    @pytest.mark.asyncio
    async def test_resume_text_provider_invoked_when_no_inline(self) -> None:
        """When ``score()`` is called without a ``resume_text`` argument
        and a provider is configured, the provider is asked for the
        resume text."""
        seen_profiles: list[SearchProfile] = []

        def provider(profile: SearchProfile) -> str:
            seen_profiles.append(profile)
            return "10 years of Python"

        response = json.dumps({"score": 50, "explanation": "ok"})
        client = InMemoryLLMClient(responses={"*": response})
        registry = _StaticPromptRegistry()
        scorer = LLMScorer(llm=client, prompt_registry=registry, resume_text_provider=provider)

        profile = _profile()
        await scorer.score(_vacancy(), profile)

        assert seen_profiles == [profile]

    @pytest.mark.asyncio
    async def test_inline_resume_text_takes_precedence(self) -> None:
        """When the caller passes ``resume_text=...`` directly, the
        provider is *not* invoked."""
        provider_called = False

        def provider(profile: SearchProfile) -> str:  # noqa: ARG001
            nonlocal provider_called
            provider_called = True
            return "from provider"

        response = json.dumps({"score": 50, "explanation": "ok"})
        client = InMemoryLLMClient(responses={"*": response})
        registry = _StaticPromptRegistry()
        scorer = LLMScorer(llm=client, prompt_registry=registry, resume_text_provider=provider)

        await scorer.score(_vacancy(), _profile(), resume_text="inline resume")

        assert provider_called is False

    @pytest.mark.asyncio
    async def test_no_resume_section_when_no_resume_available(self) -> None:
        """If neither an inline resume nor a provider is configured,
        the prompt must not contain a placeholder resume text."""
        seen: list[str] = []

        def fake(prompt: str, *, temperature: float = 0.2, max_tokens: int = 1024) -> str:
            seen.append(prompt)
            return json.dumps({"score": 50, "explanation": "ok"})

        client = InMemoryLLMClient(responses=fake)
        registry = _StaticPromptRegistry()
        scorer = LLMScorer(llm=client, prompt_registry=registry)  # no provider

        await scorer.score(_vacancy(), _profile())

        # The prompt is rendered; no leakage of an inline resume.
        assert "10 years of Python" not in seen[0]


# ---------------------------------------------------------------------------
# LLM failure modes
# ---------------------------------------------------------------------------


class TestScorerErrorPropagation:
    @pytest.mark.asyncio
    async def test_invalid_llm_response_raises(self) -> None:
        """When the LLM returns garbage, the scorer surfaces a
        :class:`LLMScoreParseError` rather than swallowing it."""
        from job_apply.features.scoring.parsing import LLMScoreParseError

        client = InMemoryLLMClient(responses={"*": "this is not json"})
        registry = _StaticPromptRegistry()
        scorer = LLMScorer(llm=client, prompt_registry=registry)

        with pytest.raises(LLMScoreParseError):
            await scorer.score(_vacancy(), _profile())


# ---------------------------------------------------------------------------
# Score clamping at the scorer level
# ---------------------------------------------------------------------------


class TestScorerClampsScore:
    @pytest.mark.asyncio
    async def test_out_of_range_score_is_clamped(self) -> None:
        """If the LLM returns 250, the scorer clamps to 100 (the parser
        does the clamping, but the scorer must still produce a valid
        :class:`ScoreResult`)."""
        response = json.dumps({"score": 250, "explanation": "wild"})
        client = InMemoryLLMClient(responses={"*": response})
        registry = _StaticPromptRegistry()
        scorer = LLMScorer(llm=client, prompt_registry=registry)

        result = await scorer.score(_vacancy(), _profile())

        assert result.score == 100


# ---------------------------------------------------------------------------
# PromptVersion dataclass
# ---------------------------------------------------------------------------


class TestPromptVersion:
    def test_carries_name_and_version(self) -> None:
        pv = PromptVersion(name="vacancy_scoring", version="v3", template="x")
        assert pv.name == "vacancy_scoring"
        assert pv.version == "v3"
        assert pv.template == "x"


# ---------------------------------------------------------------------------
# Default prompt builder is wired in
# ---------------------------------------------------------------------------


class TestDefaultPromptBuilder:
    @pytest.mark.asyncio
    async def test_scorer_uses_build_vacancy_scoring_prompt_by_default(self) -> None:
        """The scorer should pass the result of
        :func:`build_vacancy_scoring_prompt` as the LLM prompt. The
        test verifies the wiring by inspecting what the in-memory
        client received."""

        class _CapturingClient:
            def __init__(self) -> None:
                self.prompts: list[str] = []

            async def complete(
                self,
                prompt: str,
                *,
                temperature: float = 0.2,
                max_tokens: int = 1024,
            ) -> str:
                self.prompts.append(prompt)
                return json.dumps({"score": 50, "explanation": "ok"})

        client = _CapturingClient()
        registry = _StaticPromptRegistry()
        scorer = LLMScorer(llm=client, prompt_registry=registry)  # type: ignore[arg-type]

        vacancy = _vacancy()
        profile = _profile()
        await scorer.score(vacancy, profile)

        # The actual prompt is whatever build_vacancy_scoring_prompt produces.
        expected = build_vacancy_scoring_prompt(vacancy, profile)
        assert client.prompts == [expected]
