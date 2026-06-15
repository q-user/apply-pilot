"""Prompt builder for the vacancy-scoring prompt (issue #29).

The builder takes a :class:`Vacancy`, a :class:`SearchProfile`, and
optional resume text, and produces the string the LLM sees. The
resulting prompt is a single, self-contained chunk of text that:

* sets the role of the model (``You are a recruiting assistant``);
* describes the response contract (strict JSON with three fields);
* lists the vacancy fields, the profile fields, and the resume text;
* closes with a single-line "respond with JSON only" reminder.

The prompt is deliberately *not* an f-string or a Jinja template:
real LLM prompts need their prose to read naturally, and the score
*and* explanation in the contract are part of the prompt's tone
calibration. Keeping the whole thing in a single function with a
single string literal at the bottom makes it easy to read, easy to
diff, and easy to A/B test by swapping out the ``_TEMPLATE`` string.
"""

from __future__ import annotations

from job_apply.features.search_profiles.models import SearchProfile
from job_apply.features.sources.models import Vacancy

#: Header of the prompt — the system-role framing. Kept short so the
#: model can re-read it on every call.
_SYSTEM_HEADER = (
    "You are a recruiting assistant that scores how well a job "
    "vacancy matches a candidate's search profile. You must return a "
    "single JSON object on the last line of your response — no prose, "
    "no Markdown, no code fences."
)


#: Response contract. Repeated at the top *and* bottom of the prompt
#: so the model sees the shape twice and is less likely to drift.
_RESPONSE_CONTRACT = (
    "Respond with this exact JSON shape:\n"
    '{\n  "score": <integer 0-100, 100 = perfect match>,\n'
    '  "explanation": <2-3 sentences explaining the score>,\n'
    '  "confidence": <float 0.0-1.0 expressing how sure you are>\n}'
)


def _format_salary_range(salary_from: int | None, salary_to: int | None) -> str:
    """Return a human-readable salary range, or "not specified"."""
    if salary_from is None and salary_to is None:
        return "not specified"
    if salary_from is not None and salary_to is not None:
        return f"{salary_from}–{salary_to}"
    if salary_from is not None:
        return f"from {salary_from}"
    return f"up to {salary_to}"


def _format_skills(skills: list[str] | None) -> str:
    """Return a comma-joined skill list, or "not specified"."""
    if not skills:
        return "not specified"
    return ", ".join(skills)


def _format_keywords(keywords: str | None) -> str:
    """Return the raw keywords string, or "not specified"."""
    if not keywords or not keywords.strip():
        return "not specified"
    return keywords.strip()


def _format_resume_section(resume_text: str | None) -> str:
    """Return the resume section of the prompt.

    When the caller did not supply a resume, the section is omitted
    entirely — empty placeholders confuse models. When the text *is*
    supplied, it's quoted verbatim under a labelled header.
    """
    if not resume_text or not resume_text.strip():
        return ""
    return (
        "\n## CANDIDATE RESUME\n"
        "The following is the candidate's resume. Use it to evaluate\n"
        "experience fit:\n\n"
        f"{resume_text.strip()}\n"
    )


def build_vacancy_scoring_prompt(
    vacancy: Vacancy,
    profile: SearchProfile,
    *,
    resume_text: str | None = None,
) -> str:
    """Build the full LLM prompt for scoring ``vacancy`` against ``profile``.

    The returned string is meant to be sent verbatim to the LLM
    client. It is self-contained (no placeholders left) and uses a
    Markdown-ish layout (``## VACANCY``, ``## PROFILE``) so the model
    can parse it reliably across providers.

    Parameters
    ----------
    vacancy:
        The canonical vacancy to score. Only the fields documented
        below are referenced; unknown fields are ignored.
    profile:
        The user's search profile. Same.
    resume_text:
        Optional resume text. When ``None`` or empty, the resume
        section is omitted from the prompt entirely (we never emit an
        empty placeholder).
    """
    salary = _format_salary_range(vacancy.salary_from, vacancy.salary_to)
    skills = _format_skills(vacancy.skills)
    keywords = _format_keywords(profile.keywords)
    profile_salary = _format_salary_range(profile.salary_min, profile.salary_max)
    resume_section = _format_resume_section(resume_text)

    return (
        f"{_SYSTEM_HEADER}\n"
        "\n"
        f"{_RESPONSE_CONTRACT}\n"
        "\n"
        "## VACANCY\n"
        f"Title: {vacancy.title or 'not specified'}\n"
        f"Employer: {vacancy.employer_name or 'not specified'}\n"
        f"Location: {vacancy.location or 'not specified'}\n"
        f"Schedule: {vacancy.schedule or 'not specified'}\n"
        f"Experience: {vacancy.experience or 'not specified'}\n"
        f"Salary: {salary}\n"
        f"Skills: {skills}\n"
        f"Description:\n{(vacancy.description or 'not provided').strip()}\n"
        "\n"
        "## PROFILE\n"
        f"Title: {profile.title}\n"
        f"Keywords: {keywords}\n"
        f"Preferred salary: {profile_salary}\n"
        f"Preferred location: {profile.location or 'not specified'}\n"
        f"Preferred schedule: {profile.schedule or 'not specified'}\n"
        f"{resume_section}"
        "\n"
        "## TASK\n"
        "Compare the vacancy to the profile and the candidate's resume.\n"
        "Score 0 means a complete mismatch; 100 means a perfect match.\n"
        "Respond with the JSON object only — no extra text."
    )


__all__ = ["build_vacancy_scoring_prompt"]
