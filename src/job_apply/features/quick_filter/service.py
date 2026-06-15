"""Fan-out service for the quick-filter slice.

The service is the public entry point used by callers (background
jobs, future HTTP endpoints) that need to run a batch of vacancies
through the engine. It owns no business rules — it forwards to the
:class:`QuickFilterEngine` and stitches the per-pair decisions into a
flat list, optionally persisting them via the
:class:`FilterDecisionRepository`.

Design notes
------------

* **DI**: the engine and (optionally) the repository are injected, not
  constructed inline. Tests pass an engine with custom rules and a
  fake repository; production wiring builds one with
  :func:`quick_filter.rules.default_rules` and the SQLAlchemy-backed
  repository sharing the request's session.
* **Order preservation**: the outer loop in
  :meth:`evaluate_for_active_profiles` and its persist counterpart is
  the *profile* list and the inner loop is the *vacancy* list. This
  makes the output sequence stable across runs, which simplifies log
  triage.
* **No mandatory persistence**: the service still works without a
  repository — the in-memory methods return :class:`FilterDecision`
  value objects; only the ``evaluate_and_persist_*`` methods raise
  when no repository is wired so a misconfiguration is loud.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Sequence

from job_apply.features.quick_filter.engine import QuickFilterEngine
from job_apply.features.quick_filter.models import FilterDecision
from job_apply.features.quick_filter.persistence import (
    FilterDecisionRepository,
    FilterDecisionRow,
)
from job_apply.features.search_profiles.models import SearchProfile
from job_apply.features.sources.models import Vacancy


class QuickFilterService:
    """Batch entry point for the quick-filter engine."""

    __slots__ = ("_decision_repo", "_engine")

    def __init__(
        self,
        engine: QuickFilterEngine,
        *,
        decision_repo: FilterDecisionRepository | None = None,
    ) -> None:
        self._engine = engine
        self._decision_repo = decision_repo

    @property
    def engine(self) -> QuickFilterEngine:
        """Return the injected engine (read-only)."""
        return self._engine

    @property
    def decision_repo(self) -> FilterDecisionRepository | None:
        """Return the injected decision repository, or ``None``.

        The repository is optional: callers that only need the
        in-memory verdict list can build the service with the default
        ``None`` and use :meth:`evaluate_for_profile` /
        :meth:`evaluate_for_active_profiles` directly. The persist
        methods raise if the repository is missing so a
        misconfiguration is surfaced early.
        """
        return self._decision_repo

    # -- in-memory API ---------------------------------------------------

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

    # -- persistence API -------------------------------------------------

    def evaluate_and_persist_for_profile(
        self,
        vacancies: Sequence[Vacancy],
        profile: SearchProfile,
        *,
        rule_version: int = 1,
    ) -> list[FilterDecisionRow]:
        """Run the engine and persist every decision.

        Returns the list of :class:`FilterDecisionRow` rows that were
        just inserted, in the same order as the input vacancies. The
        ``reasons`` list is JSON-encoded into the ``reasons`` column so
        the storage is portable across sqlite and PostgreSQL.

        Raises :class:`RuntimeError` if no decision repository was
        injected at construction time.
        """
        repo = self._require_repo()
        if not vacancies:
            return []
        rows: list[FilterDecisionRow] = []
        for vacancy in vacancies:
            decision = self._engine.evaluate(vacancy, profile, is_strict=True)
            row = self._build_row(decision, rule_version=rule_version)
            rows.append(repo.create(row))
        return rows

    def evaluate_and_persist_for_active_profiles(
        self,
        vacancies: Sequence[Vacancy],
        profiles: Sequence[SearchProfile],
        *,
        rule_version: int = 1,
    ) -> int:
        """Run the engine across ``(vacancy, profile)`` pairs and persist.

        Returns the total number of decisions persisted. An empty
        ``vacancies`` or ``profiles`` list returns ``0`` without
        touching the repository.
        """
        self._require_repo()
        if not vacancies or not profiles:
            return 0
        total = 0
        for profile in profiles:
            total += len(
                self.evaluate_and_persist_for_profile(vacancies, profile, rule_version=rule_version)
            )
        return total

    # -- helpers ---------------------------------------------------------

    def _require_repo(self) -> FilterDecisionRepository:
        repo = self._decision_repo
        if repo is None:
            raise RuntimeError(
                "QuickFilterService.decision_repo is not configured; "
                "construct the service with `decision_repo=...` to persist."
            )
        return repo

    @staticmethod
    def _build_row(decision: FilterDecision, *, rule_version: int) -> FilterDecisionRow:
        """Translate an in-memory :class:`FilterDecision` into a row.

        The ``reasons`` list is JSON-encoded so the column is portable
        across sqlite (no native JSON type) and PostgreSQL.
        """
        if decision.vacancy_id is None or decision.profile_id is None:
            raise ValueError("FilterDecision requires non-None identifiers")
        return FilterDecisionRow(
            search_profile_id=uuid.UUID(decision.profile_id),
            vacancy_id=uuid.UUID(decision.vacancy_id),
            decision=decision.decision,
            reasons=json.dumps(decision.reasons, ensure_ascii=False),
            rule_version=rule_version,
        )


__all__ = ["QuickFilterService"]
