"""Combine :class:`FilterRule` results into a single :class:`FilterDecision`.

The engine is the only piece of the slice that knows about
``is_strict`` — rules stay pure functions of ``(vacancy, profile)`` and
the engine's job is to combine their verdicts.

Combination algorithm
--------------------

1. Walk the registered rules in registration order, calling
   ``rule.evaluate(vacancy, profile)``.
2. If ``is_strict=False`` and the rule is *soft* (it carries
   ``is_soft=True``), downgrade a ``"reject"`` verdict to
   ``"neutral"``. ``"accept"`` and ``"neutral"`` pass through
   unchanged.
3. Collect ``reason`` strings from every *surviving* ``"reject"``
   result, in evaluation order.
4. If at least one rule returned ``"reject"`` → overall decision is
   :data:`DECISION_REJECT` with the collected reasons.
5. Otherwise → :data:`DECISION_ACCEPT` with no reasons.

The :class:`FilterDecision` value object's own validators enforce the
"rejects carry at least one reason" and "accepts carry no reasons"
invariants, so the engine can rely on the dataclass to reject malformed
results.
"""

from __future__ import annotations

from collections.abc import Iterable

from apply_pilot.features.quick_filter.models import (
    DECISION_ACCEPT,
    DECISION_NEUTRAL,
    DECISION_REJECT,
    FilterDecision,
    FilterRule,
    RuleResult,
    _id_str,
)
from apply_pilot.features.search_profiles.models import SearchProfile
from apply_pilot.features.sources.models import Vacancy


class QuickFilterEngine:
    """Combine a list of :class:`FilterRule` instances into a single verdict.

    The engine holds *no* state beyond its rule list: every call to
    :meth:`evaluate` is a pure function of its arguments, which keeps
    the engine trivially safe to use concurrently from multiple
    workers (once issue #29 lands).

    Parameters
    ----------
    rules:
        Ordered iterable of rule instances. The order is preserved and
        used to determine the order of the ``reasons`` list in the
        resulting :class:`FilterDecision`. The iterable is eagerly
        materialised into a list so the engine can iterate it more than
        once (e.g. from tests asserting ``engine.rules``).
    """

    __slots__ = ("_rules",)

    def __init__(self, rules: Iterable[FilterRule]) -> None:
        self._rules: list[FilterRule] = list(rules)

    @property
    def rules(self) -> list[FilterRule]:
        """Return the registered rule list (read-only view)."""
        return list(self._rules)

    # -- public API -------------------------------------------------------

    def evaluate(
        self,
        vacancy: Vacancy,
        profile: SearchProfile,
        *,
        is_strict: bool = True,
    ) -> FilterDecision:
        """Produce a :class:`FilterDecision` for ``(vacancy, profile)``.

        Parameters
        ----------
        vacancy, profile:
            The pair to evaluate. Neither is mutated.
        is_strict:
            When ``False``, soft rules (``is_soft=True``) have their
            ``"reject"`` verdicts downgraded to ``"neutral"`` — they no
            longer contribute to the overall decision or to ``reasons``.
            Hard rules are unaffected.

        Returns
        -------
        :class:`FilterDecision`
            The overall verdict and the aggregated reasons.
        """
        vacancy_id = _id_str(vacancy.id)
        profile_id = _id_str(profile.id)
        reasons: list[str] = []

        for rule in self._rules:
            result = rule.evaluate(vacancy, profile)
            effective = self._maybe_downgrade(rule, result, is_strict=is_strict)
            if effective.decision == DECISION_REJECT:
                # ``RuleResult`` enforces a non-empty reason on reject, so
                # this branch is safe to call without a None-check.
                reasons.append(effective.reason or "")

        if reasons:
            return FilterDecision(
                vacancy_id=vacancy_id,
                profile_id=profile_id,
                decision=DECISION_REJECT,
                reasons=reasons,
            )
        return FilterDecision(
            vacancy_id=vacancy_id,
            profile_id=profile_id,
            decision=DECISION_ACCEPT,
        )

    # -- internals --------------------------------------------------------

    @staticmethod
    def _maybe_downgrade(
        rule: FilterRule,
        result: RuleResult,
        *,
        is_strict: bool,
    ) -> RuleResult:
        """Return ``result``, possibly downgraded from reject to neutral.

        A rule is bypassable when ``is_strict=False`` *and* it advertises
        itself as soft (``is_soft=True``). The downgrade is a no-op for
        ``"accept"`` and ``"neutral"`` results — we only act on the
        ``"reject"`` case so the semantic of "soft rule's reject is
        only honoured in strict mode" is preserved verbatim.
        """
        if is_strict:
            return result
        if not getattr(rule, "is_soft", False):
            return result
        if result.decision != DECISION_REJECT:
            return result
        # Downgrade: surface the rejection as neutral (and drop the
        # reason — neutral results don't carry reasons in the engine's
        # combined output).
        return RuleResult(DECISION_NEUTRAL)


__all__ = ["QuickFilterEngine"]
