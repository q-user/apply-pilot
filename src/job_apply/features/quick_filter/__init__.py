"""Quick-filter vertical slice.

The slice is a pure in-memory pre-LLM filter for ``(Vacancy,
SearchProfile)`` pairs. It answers one question: *is this vacancy even
worth scoring with the LLM?* Cheap rule-based heuristics discard
obviously-bad matches; everything else is passed up to the scoring
pipeline (issue #29).

Persistence of decisions lands in issue #28 — for now the engine and
service return in-memory :class:`FilterDecision` value objects.

Public surface
--------------

* :class:`FilterDecision` — the engine's verdict on a single pair.
* :class:`RuleResult` — the verdict of a single rule.
* :class:`FilterRule` — the Protocol implemented by every rule.
* :class:`QuickFilterEngine` — combines rule verdicts into a
  :class:`FilterDecision`; the only piece that knows about
  ``is_strict``.
* :class:`QuickFilterService` — fan-out wrapper for batch evaluations.
* :data:`default_rules` — the canonical ordered list of rules
  (``SalaryRangeRule``, ``LocationRule``, ``ScheduleRule``,
  ``KeywordRule``, ``TitleLengthRule``).
"""

from __future__ import annotations

from job_apply.features.quick_filter.engine import QuickFilterEngine
from job_apply.features.quick_filter.models import (
    DECISION_ACCEPT,
    DECISION_NEUTRAL,
    DECISION_REJECT,
    FilterDecision,
    FilterRule,
    RuleResult,
)
from job_apply.features.quick_filter.rules import (
    KeywordRule,
    LocationRule,
    SalaryRangeRule,
    ScheduleRule,
    TitleLengthRule,
    default_rules,
)
from job_apply.features.quick_filter.service import QuickFilterService

__all__ = [
    "DECISION_ACCEPT",
    "DECISION_NEUTRAL",
    "DECISION_REJECT",
    "FilterDecision",
    "FilterRule",
    "KeywordRule",
    "LocationRule",
    "QuickFilterEngine",
    "QuickFilterService",
    "RuleResult",
    "SalaryRangeRule",
    "ScheduleRule",
    "TitleLengthRule",
    "default_rules",
]
