"""Prompt template for the M3 deep LLM scoring pipeline (issue #29).

The slice is intentionally in-memory: the canonical ``vacancy_scoring``
prompt is a plain Python string constant rather than a row in a
``PromptVersion`` registry. The registry is owned by issue #30
("persist LLM score, explanation, and prompt version") and will be
introduced when the persistence story for prompt versions is
prioritised.

This module exposes:

* :data:`VACANCY_SCORING_PROMPT_V1` — the canonical ``v1`` prompt body
  the LLM receives.
* :data:`VACANCY_SCORING_PROMPT_VERSION` — the version stamp applied
  to :class:`~apply_pilot.features.scoring.llm.ScoreResult.prompt_version`.
* :func:`build_vacancy_scoring_prompt` — interpolate vacancy + profile
  (and optional resume text) into :data:`VACANCY_SCORING_PROMPT_V1`.
"""

from __future__ import annotations

from apply_pilot.features.search_profiles.models import SearchProfile
from apply_pilot.features.sources.models import Vacancy

#: Hardcoded version stamp applied to every :class:`ScoreResult` produced
#: by :class:`~apply_pilot.features.scoring.llm.LLMScorer`. Form follows
#: SemVer; a real registry will land in a follow-up issue.
VACANCY_SCORING_PROMPT_VERSION: str = "vacancy_scoring@1.0.0"

#: Canonical vacancy-scoring prompt body. The LLM is asked to respond
#: in a strict JSON shape that :func:`~apply_pilot.features.scoring.llm.parse_score_response`
#: can parse, with a small set of optional fields defaulted to safe
#: values when absent.
VACANCY_SCORING_PROMPT_V1: str = """\
You are an expert technical recruiter. Score the following vacancy
against the candidate's search profile on a 0-100 scale, where 100
means a perfect match and 0 means the vacancy is irrelevant or
actively mismatched.

Vacancy
-------
Title: {vacancy_title}
Employer: {vacancy_employer}
Location: {vacancy_location}
Schedule: {vacancy_schedule}
Experience: {vacancy_experience}
Salary: {vacancy_salary_from}-{vacancy_salary_to} {vacancy_salary_currency}{vacancy_salary_gross}
Skills: {vacancy_skills}
Description: {vacancy_description}

Search profile
--------------
Title: {profile_title}
Keywords: {profile_keywords}
Salary: {profile_salary_min}-{profile_salary_max}
Location: {profile_location}
Schedule: {profile_schedule}
{resume_section}

Respond with a single JSON object and nothing else, in the form:

{{"score": <0-100>, "explanation": "<1-3 sentences>", "confidence": <0.0-1.0>}}

Scoring rubric:
* 80-100: Excellent fit. All hard requirements satisfied; salary and
  location match the profile's preferences.
* 50-79: Decent fit. Most requirements satisfied; one or two soft
  mismatches (location, schedule, level).
* 20-49: Weak fit. Multiple mismatches or significant skill gaps.
* 0-19: Misfit. Vacancy is below the candidate's stated bar, in the
  wrong domain, or otherwise uninteresting.
"""


def _salary_gross_suffix(gross: bool) -> str:
    """Return the "(gross)" suffix for the vacancy salary line."""
    return " (gross)" if gross else " (net)"


def _skills_line(skills: list[str] | None) -> str:
    """Render the skills list as a comma-separated string."""
    return ", ".join(skills) if skills else "(not specified)"


def _resume_section(resume_text: str | None) -> str:
    """Render the optional resume block, or an empty line if not supplied."""
    if not resume_text:
        return ""
    return f"\nResume\n------\n{resume_text}\n"


def build_vacancy_scoring_prompt(
    vacancy: Vacancy,
    profile: SearchProfile,
    *,
    resume_text: str | None = None,
) -> str:
    """Render the canonical ``vacancy_scoring@v1`` prompt for one pair.

    ``resume_text`` is optional; when omitted the prompt omits the
    resume section so the LLM scores on vacancy + profile alone. Any
    non-empty string is rendered verbatim, including its whitespace.
    """
    return VACANCY_SCORING_PROMPT_V1.format(
        vacancy_title=vacancy.title,
        vacancy_employer=vacancy.employer_name or "(not specified)",
        vacancy_location=vacancy.location or "(not specified)",
        vacancy_schedule=vacancy.schedule or "(not specified)",
        vacancy_experience=vacancy.experience or "(not specified)",
        vacancy_salary_from=vacancy.salary_from if vacancy.salary_from is not None else "?",
        vacancy_salary_to=vacancy.salary_to if vacancy.salary_to is not None else "?",
        vacancy_salary_currency=vacancy.salary_currency,
        vacancy_salary_gross=_salary_gross_suffix(bool(vacancy.salary_gross)),
        vacancy_skills=_skills_line(vacancy.skills),
        vacancy_description=vacancy.description or "(not specified)",
        profile_title=profile.title,
        profile_keywords=profile.keywords or "(not specified)",
        profile_salary_min=profile.salary_min if profile.salary_min is not None else "?",
        profile_salary_max=profile.salary_max if profile.salary_max is not None else "?",
        profile_location=profile.location or "(not specified)",
        profile_schedule=profile.schedule or "(not specified)",
        resume_section=_resume_section(resume_text),
    )


__all__ = [
    "VACANCY_SCORING_PROMPT_V1",
    "VACANCY_SCORING_PROMPT_VERSION",
    "build_vacancy_scoring_prompt",
]
