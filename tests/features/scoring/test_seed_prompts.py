"""TDD tests for :func:`seed_default_prompts`.

The seed must populate the registry with the initial active versions of
the two prompt names that the M3 scoring pipeline relies on:

* ``vacancy_scoring`` — used by the LLM scoring pass (issue #29).
* ``cover_letter`` — used by the cover-letter generation slices (#31,
  #32).

The seed is idempotent: re-running it on a registry that already has
matching versions does not raise or create duplicates.
"""

from __future__ import annotations

import pytest

from job_apply.features.scoring.registry import (
    InMemoryPromptVersionRegistry,
    PromptVersion,
    seed_default_prompts,
)


@pytest.fixture
def registry() -> InMemoryPromptVersionRegistry:
    return InMemoryPromptVersionRegistry()


def test_seed_registers_vacancy_scoring(registry: InMemoryPromptVersionRegistry) -> None:
    """``vacancy_scoring`` must be present and active after the seed."""
    seed_default_prompts(registry)

    active = registry.get_active("vacancy_scoring")
    assert active is not None
    assert active.is_active is True
    assert active.template  # non-empty placeholder


def test_seed_registers_cover_letter(registry: InMemoryPromptVersionRegistry) -> None:
    """``cover_letter`` must be present and active after the seed."""
    seed_default_prompts(registry)

    active = registry.get_active("cover_letter")
    assert active is not None
    assert active.is_active is True
    assert active.template


def test_seed_registers_exactly_two_prompts(registry: InMemoryPromptVersionRegistry) -> None:
    """The seed must register exactly the two known prompt names."""
    seed_default_prompts(registry)

    names = {p.name for p in registry.list_all()}
    assert names == {"vacancy_scoring", "cover_letter"}


def test_seed_versions_follow_semver(registry: InMemoryPromptVersionRegistry) -> None:
    """Initial versions must be parseable as SemVer 2.0.0."""
    seed_default_prompts(registry)

    for prompt in registry.list_all():
        # Loose shape: starts with a digit and contains a dot.
        assert prompt.version, f"{prompt.name} has empty version"
        major, minor, *_ = prompt.version.split(".", 2)
        assert major.isdigit(), f"{prompt.name} version not SemVer: {prompt.version!r}"
        assert minor.isdigit(), f"{prompt.name} version not SemVer: {prompt.version!r}"


def test_seed_is_idempotent(registry: InMemoryPromptVersionRegistry) -> None:
    """Re-running the seed must not raise and must not duplicate rows."""
    seed_default_prompts(registry)
    first_versions = {p.name: p.version for p in registry.list_all()}

    # Re-seeding into a registry that already has those names must be a
    # no-op (the seed should detect the existing active and skip).
    seed_default_prompts(registry)

    second_versions = {p.name: p.version for p in registry.list_all()}
    assert first_versions == second_versions
    assert len(registry.list_all()) == 2


def test_seed_prompts_are_frozen_dataclasses(
    registry: InMemoryPromptVersionRegistry,
) -> None:
    """Seeded prompts are :class:`PromptVersion` frozen dataclasses."""
    seed_default_prompts(registry)

    for prompt in registry.list_all():
        assert isinstance(prompt, PromptVersion)
        with pytest.raises((AttributeError, Exception)):
            prompt.version = "9.9.9"  # type: ignore[misc]
