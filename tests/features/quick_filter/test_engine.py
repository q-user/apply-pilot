"""Failing tests for :class:`QuickFilterEngine`.

The engine composes a list of :class:`FilterRule` instances and
combines their results into a single :class:`FilterDecision`. We inject
*real* rule instances — the test exercises the production code path
end-to-end, just with controlled inputs.
"""

from __future__ import annotations

import uuid

from job_apply.features.quick_filter.engine import QuickFilterEngine
from job_apply.features.quick_filter.models import (
    DECISION_ACCEPT,
    DECISION_NEUTRAL,
    DECISION_REJECT,
    FilterDecision,
    RuleResult,
)
from job_apply.features.quick_filter.rules import (
    KeywordRule,
    LocationRule,
    SalaryRangeRule,
    ScheduleRule,
    TitleLengthRule,
)
from job_apply.features.search_profiles.models import SearchProfile
from job_apply.features.sources.models import Vacancy

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _vacancy(
    *,
    title: str = "Senior Python Developer",
    description: str | None = "Python, Django, PostgreSQL",
    salary_from: int | None = 200_000,
    location: str | None = "Москва",
    schedule: str | None = "fullDay",
) -> Vacancy:
    v = Vacancy(
        source="hh",
        source_id=str(uuid.uuid4()),
        title=title,
        description=description,
        salary_from=salary_from,
        location=location,
        schedule=schedule,
        raw_data={},
    )
    v.id = uuid.uuid4()
    return v


def _profile(
    user_id: uuid.UUID,
    *,
    keywords: str | None = None,
    salary_max: int | None = None,
    location: str | None = None,
    schedule: str | None = None,
) -> SearchProfile:
    p = SearchProfile(
        user_id=user_id,
        title="Backend",
        keywords=keywords,
        salary_max=salary_max,
        location=location,
        schedule=schedule,
        is_active=True,
    )
    p.id = uuid.uuid4()
    return p


class _AlwaysAcceptRule:
    """Spy rule that always accepts — for verifying the engine fans out to
    every registered rule."""

    name = "always_accept"
    is_soft = False
    calls: list[tuple[uuid.UUID, uuid.UUID]] = []

    def evaluate(self, vacancy: Vacancy, profile: SearchProfile) -> RuleResult:
        type(self).calls.append((vacancy.id, profile.id))
        return RuleResult(DECISION_ACCEPT)


class _AlwaysRejectRule:
    name = "always_reject"
    is_soft = False

    def evaluate(self, vacancy: Vacancy, profile: SearchProfile) -> RuleResult:  # noqa: ARG002
        return RuleResult(DECISION_REJECT, "rejected on purpose")


class _SoftRejectRule:
    name = "soft_reject"
    is_soft = True

    def evaluate(self, vacancy: Vacancy, profile: SearchProfile) -> RuleResult:  # noqa: ARG002
        return RuleResult(DECISION_REJECT, "soft reject")


class _SoftNeutralRule:
    name = "soft_neutral"
    is_soft = True

    def evaluate(self, vacancy: Vacancy, profile: SearchProfile) -> RuleResult:  # noqa: ARG002
        return RuleResult(DECISION_NEUTRAL, "no opinion")


# ---------------------------------------------------------------------------
# Combination logic
# ---------------------------------------------------------------------------


class TestEngineCombination:
    def test_accepts_when_no_rules_reject(self) -> None:
        """All rules returning accept/neutral yields an accept decision."""
        engine = QuickFilterEngine(rules=[_AlwaysAcceptRule()])
        vacancy = _vacancy()
        profile = _profile(uuid.uuid4())

        decision = engine.evaluate(vacancy, profile)

        assert decision.decision == DECISION_ACCEPT
        assert decision.reasons == []

    def test_rejects_when_any_rule_rejects(self) -> None:
        """A single reject is enough to flip the decision."""
        engine = QuickFilterEngine(rules=[_AlwaysAcceptRule(), _AlwaysRejectRule()])
        vacancy = _vacancy()
        profile = _profile(uuid.uuid4())

        decision = engine.evaluate(vacancy, profile)

        assert decision.decision == DECISION_REJECT
        assert decision.reasons == ["rejected on purpose"]

    def test_reasons_concatenated_in_evaluation_order(self) -> None:
        """Reasons follow the order in which the rejecting rules fired."""
        engine = QuickFilterEngine(
            rules=[
                _AlwaysRejectRule(),  # rule 0 — replaces name via class
                _AlwaysAcceptRule(),
                _AlwaysRejectRule(),  # second instance — but same name
            ]
        )
        # Make the second reject distinguishable by giving it a unique
        # subclass so its ``name`` attribute changes.

        class _NamedReject:
            name = "second_reject"
            is_soft = False

            def evaluate(self, vacancy: Vacancy, profile: SearchProfile) -> RuleResult:  # noqa: ARG002
                return RuleResult(DECISION_REJECT, "second reason")

        engine = QuickFilterEngine(rules=[_AlwaysRejectRule(), _NamedReject()])
        vacancy = _vacancy()
        profile = _profile(uuid.uuid4())

        decision = engine.evaluate(vacancy, profile)

        assert decision.decision == DECISION_REJECT
        assert decision.reasons == ["rejected on purpose", "second reason"]

    def test_neutral_results_do_not_contribute_reasons(self) -> None:
        """Neutral results are neither good nor bad — they don't show up."""
        engine = QuickFilterEngine(rules=[_SoftNeutralRule(), _AlwaysAcceptRule()])
        vacancy = _vacancy()
        profile = _profile(uuid.uuid4())

        decision = engine.evaluate(vacancy, profile)

        assert decision.decision == DECISION_ACCEPT
        assert decision.reasons == []

    def test_propagates_vacancy_and_profile_ids(self) -> None:
        """The decision carries the stringified vacancy/profile ids."""
        engine = QuickFilterEngine(rules=[_AlwaysAcceptRule()])
        vacancy = _vacancy()
        profile = _profile(uuid.uuid4())

        decision = engine.evaluate(vacancy, profile)

        assert decision.vacancy_id == str(vacancy.id)
        assert decision.profile_id == str(profile.id)

    def test_decision_has_created_at_timestamp(self) -> None:
        engine = QuickFilterEngine(rules=[_AlwaysAcceptRule()])

        decision = engine.evaluate(_vacancy(), _profile(uuid.uuid4()))

        assert decision.created_at.tzinfo is not None


# ---------------------------------------------------------------------------
# Strict vs non-strict
# ---------------------------------------------------------------------------


class TestStrictMode:
    def test_default_is_strict(self) -> None:
        """``evaluate`` defaults to ``is_strict=True``."""
        engine = QuickFilterEngine(rules=[_SoftRejectRule()])
        vacancy = _vacancy()
        profile = _profile(uuid.uuid4())

        decision = engine.evaluate(vacancy, profile)

        assert decision.decision == DECISION_REJECT  # soft reject still rejects

    def test_strict_mode_treats_soft_reject_as_real_reject(self) -> None:
        engine = QuickFilterEngine(rules=[_SoftRejectRule()])

        decision = engine.evaluate(_vacancy(), _profile(uuid.uuid4()), is_strict=True)

        assert decision.decision == DECISION_REJECT
        assert "soft reject" in decision.reasons

    def test_non_strict_mode_downgrades_soft_reject(self) -> None:
        engine = QuickFilterEngine(rules=[_SoftRejectRule()])

        decision = engine.evaluate(_vacancy(), _profile(uuid.uuid4()), is_strict=False)

        # Soft reject is bypassed → no hard reject → overall accept.
        assert decision.decision == DECISION_ACCEPT
        assert decision.reasons == []

    def test_non_strict_mode_does_not_bypass_hard_reject(self) -> None:
        """Hard rules are enforced regardless of ``is_strict``."""
        engine = QuickFilterEngine(rules=[_AlwaysRejectRule()])

        decision = engine.evaluate(_vacancy(), _profile(uuid.uuid4()), is_strict=False)

        assert decision.decision == DECISION_REJECT
        assert "rejected on purpose" in decision.reasons

    def test_non_strict_mode_still_rejects_when_hard_and_soft_both_reject(self) -> None:
        engine = QuickFilterEngine(rules=[_SoftRejectRule(), _AlwaysRejectRule()])

        decision = engine.evaluate(_vacancy(), _profile(uuid.uuid4()), is_strict=False)

        assert decision.decision == DECISION_REJECT
        # Only the hard reject survives; the soft reject is downgraded away.
        assert decision.reasons == ["rejected on purpose"]

    def test_strict_mode_rejects_when_soft_and_hard_both_reject(self) -> None:
        engine = QuickFilterEngine(rules=[_SoftRejectRule(), _AlwaysRejectRule()])

        decision = engine.evaluate(_vacancy(), _profile(uuid.uuid4()), is_strict=True)

        assert decision.decision == DECISION_REJECT
        assert decision.reasons == ["soft reject", "rejected on purpose"]


# ---------------------------------------------------------------------------
# Empty rules
# ---------------------------------------------------------------------------


class TestEmptyRules:
    def test_engine_with_no_rules_accepts_everything(self) -> None:
        """A vacuous engine is a no-op: any vacancy is accepted."""
        engine = QuickFilterEngine(rules=[])

        decision = engine.evaluate(_vacancy(), _profile(uuid.uuid4()))

        assert decision.decision == DECISION_ACCEPT
        assert decision.reasons == []


# ---------------------------------------------------------------------------
# Engine over the production rule set
# ---------------------------------------------------------------------------


class TestEngineWithProductionRules:
    """Smoke tests over the actual rules from :mod:`.rules`.

    They make sure the wiring is correct (DI list is iterated, reasons
    are read off the right rules, the soft keyword rule is bypassable)
    without duplicating the per-rule coverage.
    """

    def test_perfect_vacancy_is_accepted(self) -> None:
        engine = QuickFilterEngine(
            rules=[
                SalaryRangeRule(),
                LocationRule(),
                ScheduleRule(),
                KeywordRule(),
                TitleLengthRule(),
            ]
        )
        vacancy = _vacancy(
            title="Senior Python Developer",
            description="Django, FastAPI, PostgreSQL",
            salary_from=150_000,
            location="Москва",
            schedule="fullDay",
        )
        profile = _profile(
            uuid.uuid4(),
            keywords="python",
            salary_max=200_000,
            location="Москва",
            schedule="fullDay",
        )

        decision = engine.evaluate(vacancy, profile)

        assert decision.decision == DECISION_ACCEPT

    def test_keyword_mismatch_rejects_in_strict_mode(self) -> None:
        engine = QuickFilterEngine(rules=[KeywordRule()])
        # Override description to ensure the keyword does not appear in either
        # the title or the description (the helper's default description
        # contains "Python", which would make the test a false positive).
        vacancy = _vacancy(title="Sales Manager", description="B2B sales experience")
        profile = _profile(uuid.uuid4(), keywords="python")

        assert engine.evaluate(vacancy, profile).decision == DECISION_REJECT

    def test_keyword_mismatch_accepted_in_non_strict_mode(self) -> None:
        engine = QuickFilterEngine(rules=[KeywordRule()])
        vacancy = _vacancy(title="Sales Manager", description="B2B sales experience")
        profile = _profile(uuid.uuid4(), keywords="python")

        decision = engine.evaluate(vacancy, profile, is_strict=False)

        assert decision.decision == DECISION_ACCEPT

    def test_returns_filter_decision_type(self) -> None:
        engine = QuickFilterEngine(rules=[])

        result = engine.evaluate(_vacancy(), _profile(uuid.uuid4()))

        assert isinstance(result, FilterDecision)


# ---------------------------------------------------------------------------
# DI: rule list is preserved
# ---------------------------------------------------------------------------


class TestDependencyInjection:
    def test_engine_stores_rules_for_introspection(self) -> None:
        """The engine exposes its rule list so tests/observers can verify wiring."""
        rules = [SalaryRangeRule(), LocationRule(), KeywordRule()]
        engine = QuickFilterEngine(rules=rules)

        assert list(engine.rules) == rules

    def test_engine_accepts_iterable_not_just_list(self) -> None:
        """A generator should also work — the engine shouldn't require a list."""
        rules = iter([SalaryRangeRule(), KeywordRule()])
        engine = QuickFilterEngine(rules=rules)  # type: ignore[arg-type]

        # The exact collection is copied into a list internally.
        assert len(list(engine.rules)) == 2
