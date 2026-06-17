"""Value objects and contracts for the quick-filter vertical slice.

The slice is intentionally in-memory only: there is no SQLAlchemy model
for :class:`FilterDecision` in this issue. Persistence lands in issue
#28, so for now :class:`FilterDecision` is a plain dataclass that lives
in the process memory and is returned to the caller.

Public surface
--------------

* :class:`FilterDecision` — the engine's verdict on a single
  ``(vacancy, profile)`` pair, with the reasons behind the verdict.
* :class:`RuleResult` — the verdict of a single rule.
* :class:`FilterRule` — the duck-typed Protocol implemented by every
  concrete rule in :mod:`.rules`.
* Module-level decision constants — the allowed values for the
  ``decision`` field, exposed as plain strings so call-sites don't have
  to import a StrEnum just to compare.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Protocol, runtime_checkable

from apply_pilot.features.search_profiles.models import SearchProfile
from apply_pilot.features.sources.models import Vacancy

# ---------------------------------------------------------------------------
# Decision constants
# ---------------------------------------------------------------------------

#: The vacancy passes the (sub)check.
DECISION_ACCEPT: str = "accept"
#: The vacancy fails the (sub)check.
DECISION_REJECT: str = "reject"
#: The (sub)check has no opinion — used by soft rules when the profile
#: does not provide the relevant input (e.g. no keywords configured).
DECISION_NEUTRAL: str = "neutral"

_DECISIONS: frozenset[str] = frozenset({DECISION_ACCEPT, DECISION_REJECT, DECISION_NEUTRAL})


def _ensure_known_decision(value: str) -> str:
    """Return ``value`` unchanged, raising :class:`ValueError` if unknown.

    Defence-in-depth: a misbehaving rule should never be able to poison
    the engine with an unrecognised verdict.
    """
    if value not in _DECISIONS:
        raise ValueError(f"unknown decision: {value!r}")
    return value


# ---------------------------------------------------------------------------
# Rule result
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RuleResult:
    """The verdict returned by a single :class:`FilterRule`.

    ``reason`` is required when ``decision`` is ``"reject"`` (so the
    engine can aggregate explanations) and is ignored otherwise.
    """

    decision: str
    reason: str | None = None

    def __post_init__(self) -> None:
        # ``frozen=True`` means we have to mutate via ``object.__setattr__``
        # tricks — easier to validate before freezing, so we re-raise
        # here against the local copy.
        _ensure_known_decision(self.decision)
        if self.decision == DECISION_REJECT and not self.reason:
            raise ValueError("reject results must carry a reason")


# ---------------------------------------------------------------------------
# Rule Protocol
# ---------------------------------------------------------------------------


@runtime_checkable
class FilterRule(Protocol):
    """The contract every quick-filter rule must satisfy.

    The engine depends on this duck-typed surface rather than a concrete
    base class so new rules can be added without touching the engine.

    Attributes
    ----------
    name:
        Stable, human-readable identifier used in logs, the
        :class:`FilterDecision.reasons` list, and metrics. Must be unique
        per logical rule (a single rule registered twice would produce
        duplicate reasons).
    is_soft:
        ``True`` if the engine may bypass this rule's ``"reject"``
        verdict when ``is_strict=False``. The only soft rule today is
        :class:`KeywordRule`; hard rules must always be enforced.
    """

    name: str
    is_soft: bool

    def evaluate(self, vacancy: Vacancy, profile: SearchProfile) -> RuleResult: ...


# ---------------------------------------------------------------------------
# Engine decision (in-memory value object)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class FilterDecision:
    """The engine's verdict on a single ``(vacancy, profile)`` pair.

    This is a plain in-memory value object — *not* a SQLAlchemy model.
    Issue #28 will introduce a persistent equivalent; until then the
    caller is responsible for whatever persistence they need.

    Attributes
    ----------
    vacancy_id, profile_id:
        Stringified identifiers. Using ``str`` (rather than ``UUID``)
        keeps this value object decoupled from the ORM types and lets
        the engine be reused for non-UUID inputs in the future.
    decision:
        Either :data:`DECISION_ACCEPT` or :data:`DECISION_REJECT`. The
        engine never returns ``"neutral"`` at the decision level — it
        converts neutral results to accept before producing the final
        verdict.
    reasons:
        Concatenated, in evaluation order, of the reasons from every
        rule that contributed a ``"reject"`` verdict. Empty for
        accepted decisions.
    created_at:
        UTC timestamp captured when the decision was produced. Stored
        as a timezone-aware :class:`datetime` so it round-trips cleanly
        into any future persistence layer.
    """

    vacancy_id: str
    profile_id: str
    decision: str
    reasons: list[str] = field(default_factory=list)
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    def __post_init__(self) -> None:
        _ensure_known_decision(self.decision)
        if self.decision == DECISION_REJECT and not self.reasons:
            raise ValueError("reject decisions must carry at least one reason")
        if self.decision == DECISION_ACCEPT and self.reasons:
            raise ValueError("accept decisions must not carry reasons")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _id_str(value: uuid.UUID | str | None) -> str:
    """Coerce an identifier to a string for use in :class:`FilterDecision`.

    Accepts ``UUID`` (the common case) and ``str`` (already-coerced
    values) so the engine and service can be called from tests that
    pass either.
    """
    if value is None:
        raise ValueError("FilterDecision requires non-None identifiers")
    return str(value)


__all__ = [
    "DECISION_ACCEPT",
    "DECISION_NEUTRAL",
    "DECISION_REJECT",
    "FilterDecision",
    "FilterRule",
    "RuleResult",
]
