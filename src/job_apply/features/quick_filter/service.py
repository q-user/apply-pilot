"""Fan-out service for the quick-filter slice.

The service is the public entry point used by callers (background
jobs, future HTTP endpoints) that need to run a batch of vacancies
through the engine. It owns no business rules — it forwards to the
:class:`QuickFilterEngine` and stitches the per-pair decisions into a
flat list.

Design notes
------------

* **DI**: the engine is injected, not constructed inline. Tests pass
  an engine with custom rules; production wiring builds one with
  :func:`quick_filter.rules.default_rules`.
* **Order preservation**: the outer loop in
  :meth:`evaluate_for_active_profiles` is the *profile* list and the
  inner loop is the *vacancy* list. This makes the output sequence
  stable across runs, which simplifies log triage.
* **No persistence**: issue #28 will add a ``FilterDecisionRepository``
  for storing decisions; the service today is pure in-memory.
"""

from __future__ import annotations

from collections.abc import Sequence

from job_apply.features.quick_filter.engine import QuickFilterEngine
from job_apply.features.quick_filter.models import FilterDecision
from job_apply.features.search_profiles.models import SearchProfile
from job_apply.features.sources.models import Vacancy


class QuickFilterService:
    """Batch entry point for the quick-filter engine."""

    __slots__ = ("_engine",)

    def __init__(self, engine: QuickFilterEngine) -> None:
        self._engine = engine

    @property
    def engine(self) -> QuickFilterEngine:
        """Return the injected engine (read-only)."""
        return self._engine

    # -- public API -------------------------------------------------------

    def evaluate_for_profile(
        self,
        vacancies: Sequence[Vacancy],
        profile: SearchProfile,
        *,
        is_strict: bool = True,
    ) -> list[FilterDecision]:
        """Run the engine on every vacancy against ``profile``.

        Returns one :class:`FilterDecision` per vacancy, in the same
        order as the input sequence. An empty ``vacancies`` list yields
        an empty result rather than raising.
        """
        return [
            self._engine.evaluate(vacancy, profile, is_strict=is_strict) for vacancy in vacancies
        ]

    def evaluate_for_active_profiles(
        self,
        vacancies: Sequence[Vacancy],
        profiles: Sequence[SearchProfile],
    ) -> list[FilterDecision]:
        """Run the engine on every ``(vacancy, profile)`` pair.

        The caller is expected to pass only *active* profiles; the
        service does not filter on ``is_active`` so the caller can plug
        in a custom "what counts as active" definition (e.g. a feature
        flag flipping inactive profiles on for a specific run).

        Output order: outer = ``profiles``, inner = ``vacancies``. Both
        sequences are iterated exactly once.
        """
        if not vacancies or not profiles:
            return []
        decisions: list[FilterDecision] = []
        # ``is_strict`` is a hard-coded ``True`` here: the bulk path
        # always runs in strict mode. Callers that need non-strict
        # semantics must call :meth:`evaluate_for_profile` per profile.
        for profile in profiles:
            for vacancy in vacancies:
                decisions.append(self._engine.evaluate(vacancy, profile, is_strict=True))
        return decisions


__all__ = ["QuickFilterService"]
