"""In-memory prompt-version registry for the scoring slice (issue #29).

A :class:`PromptVersionRegistry` returns the *active* :class:`PromptVersion`
for a given logical name. The LLM scorer
(:class:`job_apply.features.scoring.scorer.LLMScorer`) only depends on
the :meth:`get` surface, so a thin dict-backed implementation is
sufficient for tests and for the seed-on-startup flow. A SQL-backed
implementation can land in a follow-up issue once the matches-related
persistence work settles.

The :func:`seed_default_prompts` helper registers the canonical
``"vacancy_scoring"`` prompt with version ``"v1"``. Callers invoke it
once at process start (e.g. in a FastAPI startup hook or a CLI
command) so a fresh registry always has the production defaults.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from job_apply.features.scoring.scorer import PromptVersion


@runtime_checkable
class PromptVersionRegistry(Protocol):
    """The minimal interface :class:`LLMScorer` depends on for prompts.

    The registry owns the active version of each named prompt. The
    scorer only needs :meth:`get`; concrete implementations may add
    :meth:`register`, :meth:`list_versions`, etc.
    """

    def get(self, name: str) -> PromptVersion: ...


@runtime_checkable
class PromptVersionWriter(Protocol):
    """Optional writer-side of the prompt registry.

    :class:`LLMScorer` only needs the read side (:meth:`get`); the
    write side is used by :func:`seed_default_prompts` and admin
    tooling. Most concrete registries satisfy both contracts; the
    split exists so a read-only proxy (e.g. one shipped as a class
    attribute) can still type-check.
    """

    def get(self, name: str) -> PromptVersion: ...
    def register(self, prompt: PromptVersion) -> None: ...


#: The canonical name for the vacancy-scoring prompt. Mirrored in
#: :attr:`LLMScorer.PROMPT_NAME` so a single source of truth exists.
VACANCY_SCORING_PROMPT_NAME: str = "vacancy_scoring"

#: The version label the default prompt ships with. Bumping the
#: version is how operators invalidate scores produced by an older
#: template.
VACANCY_SCORING_DEFAULT_VERSION: str = "v1"

#: The default template body. Kept as a module-level constant so it
#: is trivially visible to operators looking for the canonical
#: prompt text. The runtime prompt the LLM sees is built by
#: :func:`job_apply.features.scoring.prompts.build_vacancy_scoring_prompt`;
#: the field on :class:`PromptVersion` is here for future prompts
#: that may need raw template access.
VACANCY_SCORING_DEFAULT_TEMPLATE: str = (
    "vacancy_scoring — renders via job_apply.features.scoring.prompts.build_vacancy_scoring_prompt"
)


class InMemoryPromptVersionRegistry:
    """Dict-backed :class:`PromptVersionRegistry` for tests and seeding.

    The registry stores a single active :class:`PromptVersion` per
    name. There is intentionally no "list all versions" surface here
    — the scorer only ever asks for the active one. Future evolution
    (e.g. A/B testing two versions) can add a richer registry without
    breaking the contract.

    The class satisfies the :class:`PromptVersionRegistry` Protocol
    structurally; no inheritance is required.
    """

    __slots__ = ("_prompts",)

    def __init__(self) -> None:
        self._prompts: dict[str, PromptVersion] = {}

    def register(self, prompt: PromptVersion) -> None:
        """Register (or overwrite) the active version for ``prompt.name``.

        A duplicate ``(name, version)`` pair is allowed: the registry
        is "last writer wins", which is the simplest behaviour for a
        startup-time seed.
        """
        if not prompt.name:
            raise ValueError("PromptVersion.name must be a non-empty string")
        if not prompt.version:
            raise ValueError("PromptVersion.version must be a non-empty string")
        self._prompts[prompt.name] = prompt

    def get(self, name: str) -> PromptVersion:
        """Return the active :class:`PromptVersion` for ``name``.

        Raises
        ------
        KeyError
            No prompt is registered under ``name``. We use ``KeyError``
            rather than a custom error type so callers can use a
            single try/except for "missing thing in the registry" —
            the message is the diagnostic.
        """
        try:
            return self._prompts[name]
        except KeyError as exc:
            raise KeyError(f"no active prompt registered for {name!r}") from exc

    def has(self, name: str) -> bool:
        """Return ``True`` if a prompt is registered under ``name``."""
        return name in self._prompts

    def __contains__(self, name: object) -> bool:
        return isinstance(name, str) and name in self._prompts


def seed_default_prompts(
    registry: PromptVersionWriter,
    *,
    version: str = VACANCY_SCORING_DEFAULT_VERSION,
    template: str = VACANCY_SCORING_DEFAULT_TEMPLATE,
) -> PromptVersion:
    """Register the canonical ``vacancy_scoring`` prompt on ``registry``.

    The helper is idempotent: calling it twice with the same
    arguments leaves the registry in the same state. Calling it with
    a different ``version`` swaps the active version, which is the
    supported way to roll forward.

    Parameters
    ----------
    registry:
        Any object that satisfies the :class:`PromptVersionWriter`
        Protocol. Both :class:`InMemoryPromptVersionRegistry` and a
        future SQL-backed implementation accept this call.
    version:
        Version label to attach to the seed. Defaults to
        :data:`VACANCY_SCORING_DEFAULT_VERSION`.
    template:
        Template body. The default points at the runtime prompt
        builder; pass a different string when A/B testing template
        bodies that are *not* the canonical one.

    Returns
    -------
    PromptVersion
        The prompt that was just registered. Returning it makes the
        call site self-documenting: ``prompt = seed_default_prompts(...)``.
    """
    prompt = PromptVersion(
        name=VACANCY_SCORING_PROMPT_NAME,
        version=version,
        template=template,
    )
    registry.register(prompt)
    return prompt


__all__ = [
    "InMemoryPromptVersionRegistry",
    "PromptVersionRegistry",
    "PromptVersionWriter",
    "VACANCY_SCORING_DEFAULT_TEMPLATE",
    "VACANCY_SCORING_DEFAULT_VERSION",
    "VACANCY_SCORING_PROMPT_NAME",
    "seed_default_prompts",
]
