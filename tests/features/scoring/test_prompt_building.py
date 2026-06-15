"""Failing tests for the vacancy-scoring prompt builder (issue #29).

The prompt builder is the only place where the ``vacancy``, ``profile``,
and optional ``resume_text`` are stitched together. Tests verify that
every key field shows up verbatim (so a regression cannot silently drop
a field that the model relies on) and that empty inputs degrade
gracefully.
"""

from __future__ import annotations

import uuid

from job_apply.features.scoring.prompts import build_vacancy_scoring_prompt
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


# ---------------------------------------------------------------------------
# Required fields
# ---------------------------------------------------------------------------


class TestPromptContainsVacancyFields:
    def test_includes_vacancy_title(self) -> None:
        prompt = build_vacancy_scoring_prompt(_vacancy(), _profile())
        assert "Senior Python Developer" in prompt

    def test_includes_vacancy_description(self) -> None:
        prompt = build_vacancy_scoring_prompt(_vacancy(), _profile())
        assert "Django, FastAPI, PostgreSQL" in prompt

    def test_includes_employer_name(self) -> None:
        prompt = build_vacancy_scoring_prompt(_vacancy(), _profile())
        assert "Acme Inc" in prompt

    def test_includes_skills(self) -> None:
        prompt = build_vacancy_scoring_prompt(_vacancy(), _profile())
        assert "Python" in prompt
        assert "Django" in prompt
        assert "PostgreSQL" in prompt

    def test_includes_salary(self) -> None:
        prompt = build_vacancy_scoring_prompt(_vacancy(), _profile())
        assert "250000" in prompt or "250 000" in prompt


class TestPromptContainsProfileFields:
    def test_includes_profile_title(self) -> None:
        prompt = build_vacancy_scoring_prompt(_vacancy(), _profile())
        assert "Backend Python" in prompt

    def test_includes_keywords(self) -> None:
        prompt = build_vacancy_scoring_prompt(_vacancy(), _profile())
        assert "python" in prompt
        assert "fastapi" in prompt
        assert "postgres" in prompt

    def test_includes_salary_range(self) -> None:
        prompt = build_vacancy_scoring_prompt(_vacancy(), _profile())
        assert "200000" in prompt or "200 000" in prompt
        assert "400000" in prompt or "400 000" in prompt

    def test_includes_location(self) -> None:
        prompt = build_vacancy_scoring_prompt(_vacancy(), _profile())
        assert "Москва" in prompt


# ---------------------------------------------------------------------------
# Resume text
# ---------------------------------------------------------------------------


class TestResumeText:
    def test_includes_resume_text_when_provided(self) -> None:
        prompt = build_vacancy_scoring_prompt(
            _vacancy(), _profile(), resume_text="10 years of Python experience"
        )
        assert "10 years of Python experience" in prompt

    def test_omits_resume_section_when_not_provided(self) -> None:
        """When no resume is supplied we should not invent one — the
        prompt must not carry an empty resume section that confuses the
        model."""
        prompt = build_vacancy_scoring_prompt(_vacancy(), _profile())
        assert "10 years of Python experience" not in prompt
        # No stale resume header with no body.
        assert "RESUME" not in prompt or "Not provided" in prompt


# ---------------------------------------------------------------------------
# Response contract
# ---------------------------------------------------------------------------


class TestPromptRequestsJson:
    def test_instructs_to_respond_with_json(self) -> None:
        prompt = build_vacancy_scoring_prompt(_vacancy(), _profile())
        # The model needs to know the expected response shape; we look
        # for the keyword ``json`` (case-insensitive) in the prompt.
        assert "json" in prompt.lower()

    def test_includes_score_field_name(self) -> None:
        prompt = build_vacancy_scoring_prompt(_vacancy(), _profile())
        assert "score" in prompt.lower()

    def test_includes_explanation_field_name(self) -> None:
        prompt = build_vacancy_scoring_prompt(_vacancy(), _profile())
        assert "explanation" in prompt.lower()


# ---------------------------------------------------------------------------
# Degradation
# ---------------------------------------------------------------------------


class TestPromptDegradation:
    def test_handles_missing_vacancy_description(self) -> None:
        prompt = build_vacancy_scoring_prompt(_vacancy(description=None), _profile())
        # No exception; the prompt is still rendered.
        assert "Senior Python Developer" in prompt

    def test_handles_missing_vacancy_skills(self) -> None:
        prompt = build_vacancy_scoring_prompt(_vacancy(skills=None), _profile())
        # No exception; the prompt is still rendered.
        assert "Senior Python Developer" in prompt

    def test_handles_missing_profile_keywords(self) -> None:
        prompt = build_vacancy_scoring_prompt(_vacancy(), _profile(keywords=None))
        assert "Senior Python Developer" in prompt
