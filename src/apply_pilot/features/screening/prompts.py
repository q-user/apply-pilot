"""Prompt template for the M3 screening-question answer pipeline (issue #34).

The slice is intentionally in-memory: the canonical ``screening_answer``
prompt is a plain Python string constant rather than a row in a
``PromptVersion`` registry. The :mod:`features.scoring.registry` surface
is owned by the scoring slice and is reused as-is (no separate
registry for screening — the prompt is small, deterministic, and
rarely changes).

This module exposes:

* :data:`SCREENING_ANSWER_PROMPT_V1` — the canonical ``v1`` prompt
  body the LLM receives.
* :data:`SCREENING_ANSWER_PROMPT_VERSION` — the version stamp applied
  to :class:`~apply_pilot.features.screening.models.ScreeningQuestionAnswer.prompt_version`.
* :func:`build_screening_answer_prompt` — interpolate question +
  resume (and optional vacancy context) into
  :data:`SCREENING_ANSWER_PROMPT_V1`.

The canonical version stamp is the one stored on every persisted
``ScreeningQuestionAnswer`` row. The service's default
``prompt_version`` argument defaults to the same string so a
regenerate does not silently bump the version.
"""

from __future__ import annotations

#: Hardcoded version stamp applied to every
#: :class:`~apply_pilot.features.screening.models.ScreeningQuestionAnswer`
#: produced by :class:`~apply_pilot.features.screening.service.ScreeningService`.
#: Form follows SemVer: ``<name>@<major>.<minor>.<patch>``.
SCREENING_ANSWER_PROMPT_VERSION: str = "screening_answer@1.0.0"

#: Canonical screening-answer prompt body. The LLM is asked to write a
#: concise answer (3-6 sentences) to a single screening question,
#: grounded in the candidate's resume and, when supplied, the
#: vacancy's context. The expected output is plain text — no JSON
#: envelope, no markdown headers.
SCREENING_ANSWER_PROMPT_V1: str = """\
You are helping a job candidate write a concise, first-person answer
to a screening question that a hiring team will read in their
applicant tracking system.

Screening question
------------------
{question}

Candidate resume
----------------
{resume_text}
{vacancy_context}

Write a single answer (3-6 sentences) that:

* speaks in the first person ("I …");
* is grounded in the resume — cite concrete years, projects, or
  technologies when they fit;
* is honest about gaps rather than inventing experience;
* avoids generic filler ("I am a hard worker", "I love this
  company") and instead makes a specific point;
* matches the language of the question (English → English,
  Russian → Russian).

Return the answer text and nothing else — no preamble, no
"Sure, here is your answer", no markdown headers.
"""


def _vacancy_context_block(vacancy_context: str | None) -> str:
    """Render the optional vacancy-context block.

    The block is appended to the resume section as additional
    information the LLM can lean on. When no context is supplied the
    block is empty (no separator line), so the rendered prompt stays
    resume-only.
    """
    if not vacancy_context:
        return ""
    return f"\nVacancy context\n---------------\n{vacancy_context}\n"


def build_screening_answer_prompt(
    question: str,
    resume_text: str,
    *,
    vacancy_context: str | None = None,
) -> str:
    """Render the canonical ``screening_answer@v1`` prompt for one question.

    Parameters
    ----------
    question
        The screening-question text the candidate needs to answer.
        Rendered verbatim into the prompt.
    resume_text
        The candidate's plain-text resume. Rendered verbatim — any
        whitespace is preserved. An empty string is allowed; the
        LLM will still produce a (much shorter) answer.
    vacancy_context
        Optional free-form vacancy context (e.g. the title, employer,
        or a short blurb). When supplied it is appended as a separate
        block under the resume. When ``None`` the block is omitted.
    """
    return SCREENING_ANSWER_PROMPT_V1.format(
        question=question,
        resume_text=resume_text or "(no resume provided)",
        vacancy_context=_vacancy_context_block(vacancy_context),
    )


__all__ = [
    "SCREENING_ANSWER_PROMPT_V1",
    "SCREENING_ANSWER_PROMPT_VERSION",
    "build_screening_answer_prompt",
]
