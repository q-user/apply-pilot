"""TDD tests for the screening-question prompt builder (M3, issue #34).

The prompt builder is the only place in the ``features/screening``
slice that knows how to render the canonical ``screening_answer@v1``
template with the question text, the candidate's resume, and an
optional vacancy context block.

The tests focus on observable behaviour:

* the rendered prompt includes the question text verbatim;
* the rendered prompt includes the resume text verbatim;
* an optional ``vacancy_context`` block is rendered when supplied
  and omitted entirely when it is not;
* the canonical version stamp is exported as a module constant.

No LLM call is made; the prompt builder is a pure function.
"""

from __future__ import annotations

from apply_pilot.features.screening.prompts import (
    SCREENING_ANSWER_PROMPT_V1,
    SCREENING_ANSWER_PROMPT_VERSION,
    build_screening_answer_prompt,
)


class TestBuildScreeningAnswerPrompt:
    def test_includes_question_text(self) -> None:
        """The question text must appear verbatim in the rendered prompt."""
        prompt = build_screening_answer_prompt(
            question="Why do you want to work here?",
            resume_text="Senior Python developer, 8 years of experience.",
        )
        assert "Why do you want to work here?" in prompt

    def test_includes_resume_text(self) -> None:
        """The candidate's resume text must appear verbatim."""
        resume = "Senior Python developer, 8 years of experience."
        prompt = build_screening_answer_prompt(
            question="Why do you want to work here?",
            resume_text=resume,
        )
        assert resume in prompt

    def test_vacancy_context_block_appears_when_supplied(self) -> None:
        """An explicit vacancy_context block is rendered into the prompt."""
        context = "Acme Corp is hiring a Senior Python Developer (remote, EU timezone)."
        prompt = build_screening_answer_prompt(
            question="Why do you want to work here?",
            resume_text="resume",
            vacancy_context=context,
        )
        assert context in prompt

    def test_vacancy_context_block_omitted_when_none(self) -> None:
        """When no context is supplied the prompt stays resume-only."""
        prompt = build_screening_answer_prompt(
            question="Tell us about your experience with X.",
            resume_text="resume",
            vacancy_context=None,
        )
        # The question and resume must still be there.
        assert "Tell us about your experience with X." in prompt
        assert "resume" in prompt

    def test_renders_canonical_template(self) -> None:
        """The builder is a thin wrapper around the canonical template."""
        rendered = build_screening_answer_prompt(
            question="Q?",
            resume_text="R",
        )
        # The canonical template must contain the same question and
        # resume placeholders; rendering substitutes them.
        assert "{question}" in SCREENING_ANSWER_PROMPT_V1
        assert "{resume_text}" in SCREENING_ANSWER_PROMPT_V1
        assert "{vacancy_context}" in SCREENING_ANSWER_PROMPT_V1
        assert "Q?" in rendered
        assert "R" in rendered

    def test_version_constant_is_semver(self) -> None:
        """The exported version stamp follows the canonical ``name@semver`` shape."""
        assert SCREENING_ANSWER_PROMPT_VERSION == "screening_answer@1.0.0"
