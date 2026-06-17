"""Quick-filter vertical slice.

The slice is a cheap rule-based pre-LLM filter for ``(Vacancy,
SearchProfile)`` pairs. It answers one question: *is this vacancy even
worth scoring with the LLM?* Cheap heuristics discard obviously-bad
matches; everything else is passed up to the scoring pipeline (issue
#29).

Persistence of decisions landed in issue #28: every engine verdict can
now be persisted as a :class:`FilterDecisionRow` via the injected
:class:`FilterDecisionRepository`.

Public surface
--------------

* :class:`FilterDecision` — the engine's in-memory verdict on a single
  pair.
* :class:`RuleResult` — the verdict of a single rule.
* :class:`FilterRule` — the Protocol implemented by every rule.
* :class:`QuickFilterEngine` — combines rule verdicts into a
  :class:`FilterDecision`; the only piece that knows about
  ``is_strict``.
* :class:`QuickFilterService` — fan-out wrapper for batch evaluations;
  with an injected repository it can also persist decisions.
* :class:`FilterDecisionRow` — the SQLAlchemy row that mirrors the
  in-memory :class:`FilterDecision`.
* :class:`FilterDecisionRepository` — the persistence Protocol.
* :data:`default_rules` — the canonical ordered list of rules
  (``SalaryRangeRule``, ``LocationRule``, ``ScheduleRule``,
  ``KeywordRule``, ``TitleLengthRule``).
"""

from __future__ import annotations

from apply_pilot.features.quick_filter.engine import QuickFilterEngine
from apply_pilot.features.quick_filter.models import (
    DECISION_ACCEPT,
    DECISION_NEUTRAL,
    DECISION_REJECT,
    FilterDecision,
    FilterRule,
    RuleResult,
)
from apply_pilot.features.quick_filter.persistence import (
    FilterDecisionRepository,
    FilterDecisionRow,
    InMemoryFilterDecisionRepository,
    SqlFilterDecisionRepository,
)
from apply_pilot.features.quick_filter.rules import (
    KeywordRule,
    LocationRule,
    SalaryRangeRule,
    ScheduleRule,
    TitleLengthRule,
    default_rules,
)
from apply_pilot.features.quick_filter.service import QuickFilterService

__all__ = [
    "DECISION_ACCEPT",
    "DECISION_NEUTRAL",
    "DECISION_REJECT",
    "FilterDecision",
    "FilterDecisionRepository",
    "FilterDecisionRow",
    "FilterRule",
    "InMemoryFilterDecisionRepository",
    "KeywordRule",
    "LocationRule",
    "QuickFilterEngine",
    "QuickFilterService",
    "RuleResult",
    "SalaryRangeRule",
    "ScheduleRule",
    "SqlFilterDecisionRepository",
    "TitleLengthRule",
    "default_rules",
]
