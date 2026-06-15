"""Tests for the prompt-version registry (issue #29).

The registry is the source of truth for "which prompt version is
currently active". The tests use the in-memory implementation
directly; a SQL-backed implementation can land in a follow-up issue
and reuse the same :class:`PromptVersionRegistry` Protocol.
"""

from __future__ import annotations

import pytest

from job_apply.features.scoring import (
    VACANCY_SCORING_DEFAULT_VERSION,
    VACANCY_SCORING_PROMPT_NAME,
    InMemoryPromptVersionRegistry,
    PromptVersion,
    PromptVersionRegistry,
    seed_default_prompts,
)

# ---------------------------------------------------------------------------
# InMemoryPromptVersionRegistry
# ---------------------------------------------------------------------------


class TestInMemoryRegistry:
    def test_register_and_get(self) -> None:
        reg = InMemoryPromptVersionRegistry()
        reg.register(PromptVersion(name="x", version="v1", template="t"))

        result = reg.get("x")

        assert result.name == "x"
        assert result.version == "v1"
        assert result.template == "t"

    def test_get_missing_raises(self) -> None:
        reg = InMemoryPromptVersionRegistry()

        with pytest.raises(KeyError):
            reg.get("missing")

    def test_register_overwrites(self) -> None:
        reg = InMemoryPromptVersionRegistry()
        reg.register(PromptVersion(name="x", version="v1", template="t1"))
        reg.register(PromptVersion(name="x", version="v2", template="t2"))

        result = reg.get("x")

        assert result.version == "v2"
        assert result.template == "t2"

    def test_register_validates_name(self) -> None:
        reg = InMemoryPromptVersionRegistry()

        with pytest.raises(ValueError):
            reg.register(PromptVersion(name="", version="v1", template="t"))

    def test_register_validates_version(self) -> None:
        reg = InMemoryPromptVersionRegistry()

        with pytest.raises(ValueError):
            reg.register(PromptVersion(name="x", version="", template="t"))

    def test_has(self) -> None:
        reg = InMemoryPromptVersionRegistry()
        reg.register(PromptVersion(name="x", version="v1", template="t"))

        assert reg.has("x") is True
        assert reg.has("missing") is False

    def test_contains(self) -> None:
        reg = InMemoryPromptVersionRegistry()
        reg.register(PromptVersion(name="x", version="v1", template="t"))

        assert "x" in reg
        assert "missing" not in reg

    def test_satisfies_protocol(self) -> None:
        """A :class:`PromptVersionRegistry` Protocol variable should
        accept the in-memory implementation."""
        reg: PromptVersionRegistry = InMemoryPromptVersionRegistry()
        assert reg is not None


# ---------------------------------------------------------------------------
# seed_default_prompts
# ---------------------------------------------------------------------------


class TestSeedDefaultPrompts:
    def test_registers_vacancy_scoring(self) -> None:
        reg = InMemoryPromptVersionRegistry()

        prompt = seed_default_prompts(reg)

        assert prompt.name == VACANCY_SCORING_PROMPT_NAME
        assert prompt.version == VACANCY_SCORING_DEFAULT_VERSION
        assert reg.has(VACANCY_SCORING_PROMPT_NAME)
        assert reg.get(VACANCY_SCORING_PROMPT_NAME).version == VACANCY_SCORING_DEFAULT_VERSION

    def test_custom_version(self) -> None:
        reg = InMemoryPromptVersionRegistry()

        prompt = seed_default_prompts(reg, version="v42")

        assert prompt.version == "v42"
        assert reg.get(VACANCY_SCORING_PROMPT_NAME).version == "v42"

    def test_custom_template(self) -> None:
        reg = InMemoryPromptVersionRegistry()

        prompt = seed_default_prompts(reg, template="custom template body")

        assert prompt.template == "custom template body"

    def test_idempotent(self) -> None:
        """Calling seed twice with the same args leaves the registry
        in the same state."""
        reg = InMemoryPromptVersionRegistry()
        first = seed_default_prompts(reg)
        second = seed_default_prompts(reg)

        assert first == second
        # Only one active version (the latest write).
        assert reg.get(VACANCY_SCORING_PROMPT_NAME) == first

    def test_satisfies_protocol(self) -> None:
        """A :class:`PromptVersionWriter` Protocol variable should
        accept the in-memory implementation."""
        from job_apply.features.scoring.registry import PromptVersionWriter

        reg: PromptVersionWriter = InMemoryPromptVersionRegistry()
        assert reg is not None
