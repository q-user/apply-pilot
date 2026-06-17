"""Concrete :class:`FilterRule` implementations.

Each rule is a small, pure function of ``(vacancy, profile)``: it
reads the relevant fields and returns a :class:`RuleResult`. The rules
are deliberately stateless and side-effect free so the engine can call
them in any order and tests can instantiate them without fixtures.

Soft rule contract
------------------

Only :class:`KeywordRule` is *soft*: when it cannot find a configured
keyword in the vacancy text it returns ``"reject"``, but the engine
will downgrade that verdict to ``"neutral"`` (effectively accept)
when ``is_strict=False``. The ``is_soft`` class attribute is the signal
the engine keys off; new soft rules must set it to ``True``.
"""

from __future__ import annotations

from apply_pilot.features.quick_filter.models import (
    DECISION_ACCEPT,
    DECISION_NEUTRAL,
    DECISION_REJECT,
    RuleResult,
)
from apply_pilot.features.search_profiles.models import SearchProfile
from apply_pilot.features.sources.models import Vacancy

#: How much above the profile's ceiling a vacancy may ask and still pass.
#: ``1.1`` means "up to 10% over the user's max" — small enough to
#: absorb rounding differences in source data, large enough to not
#: discard attractive roles.
SALARY_SLACK: float = 1.1

#: Minimum acceptable ``len(title.strip())``. Anything shorter is
#: rejected as "obviously bad data" (we cannot score a title we
#: cannot read).
MIN_TITLE_LENGTH: int = 3


# ---------------------------------------------------------------------------
# Salary range
# ---------------------------------------------------------------------------


class SalaryRangeRule:
    """Reject vacancies whose floor salary exceeds the profile's ceiling.

    A 10% slack is allowed to absorb rounding/normalisation noise; only
    vacancies asking for *clearly* more than the user wants are filtered
    out. Missing data on either side is treated as "no constraint" and
    passes through — the rule is conservative about what it rejects.
    """

    name: str = "salary_range"
    is_soft: bool = False

    def evaluate(self, vacancy: Vacancy, profile: SearchProfile) -> RuleResult:
        if vacancy.salary_from is None or profile.salary_max is None:
            return RuleResult(DECISION_ACCEPT)
        threshold = profile.salary_max * SALARY_SLACK
        if vacancy.salary_from > threshold:
            return RuleResult(
                DECISION_REJECT,
                (
                    f"vacancy salary_from {vacancy.salary_from} exceeds profile "
                    f"salary_max {profile.salary_max} (with {SALARY_SLACK:.0%} slack)"
                ),
            )
        return RuleResult(DECISION_ACCEPT)


# ---------------------------------------------------------------------------
# Location
# ---------------------------------------------------------------------------


class LocationRule:
    """Reject vacancies whose location does not contain the profile's.

    The match is a case-insensitive substring: ``profile.location ==
    "Москва"`` accepts ``"Москва, ул. Тверская"``. An empty profile
    location means "no preference" and the rule passes through; a
    missing vacancy location fails when the profile does have a
    preference.
    """

    name: str = "location"
    is_soft: bool = False

    def evaluate(self, vacancy: Vacancy, profile: SearchProfile) -> RuleResult:
        wanted = (profile.location or "").strip().lower()
        if not wanted:
            return RuleResult(DECISION_ACCEPT)
        actual = (vacancy.location or "").lower()
        if wanted in actual:
            return RuleResult(DECISION_ACCEPT)
        return RuleResult(
            DECISION_REJECT,
            (
                f"vacancy location {vacancy.location!r} does not contain "
                f"profile location {profile.location!r}"
            ),
        )


# ---------------------------------------------------------------------------
# Schedule
# ---------------------------------------------------------------------------


class ScheduleRule:
    """Reject vacancies whose schedule is not the one the profile wants.

    Strict equality — the source normaliser is expected to produce a
    small, canonical set of schedule codes (``fullDay``, ``remote``,
    ``shift``, ``flexible``) so case-sensitive equality is sufficient.
    """

    name: str = "schedule"
    is_soft: bool = False

    def evaluate(self, vacancy: Vacancy, profile: SearchProfile) -> RuleResult:
        if profile.schedule is None:
            return RuleResult(DECISION_ACCEPT)
        if vacancy.schedule is None or vacancy.schedule != profile.schedule:
            return RuleResult(
                DECISION_REJECT,
                (
                    f"vacancy schedule {vacancy.schedule!r} does not match "
                    f"profile schedule {profile.schedule!r}"
                ),
            )
        return RuleResult(DECISION_ACCEPT)


# ---------------------------------------------------------------------------
# Keywords (soft)
# ---------------------------------------------------------------------------


def _parse_keywords(raw: str | None) -> list[str]:
    """Split a profile's comma-separated ``keywords`` into trimmed terms.

    Empty fragments are dropped so ``"python, , django"`` is treated
    the same as ``"python, django"``.
    """
    if not raw:
        return []
    return [part.strip() for part in raw.split(",") if part.strip()]


class KeywordRule:
    """Reject vacancies that match none of the profile's keywords.

    Soft rule: a profile without any keywords configured has no
    opinion (``neutral``), and a mismatch is *also* a soft reject —
    the engine bypasses it when ``is_strict=False`` so a new profile
    with aggressive keywords does not over-filter the candidate pool.
    """

    name: str = "keyword"
    is_soft: bool = True

    @staticmethod
    def _match_in_text(keyword: str, *texts: str | None) -> bool:
        needle = keyword.strip().lower()
        if not needle:
            return False
        return any(text and needle in text.lower() for text in texts)

    def evaluate(self, vacancy: Vacancy, profile: SearchProfile) -> RuleResult:
        terms = _parse_keywords(profile.keywords)
        if not terms:
            return RuleResult(DECISION_NEUTRAL, reason="profile has no keywords configured")
        for term in terms:
            if self._match_in_text(term, vacancy.title, vacancy.description):
                return RuleResult(DECISION_ACCEPT)
        return RuleResult(
            DECISION_REJECT,
            (
                f"vacancy title/description does not contain any of the "
                f"profile keywords ({', '.join(terms)!r})"
            ),
        )


# ---------------------------------------------------------------------------
# Title length
# ---------------------------------------------------------------------------


class TitleLengthRule:
    """Reject vacancies whose title is too short to score reliably.

    Catches ingestion bugs (empty placeholders, single-letter titles)
    *before* the LLM scorer burns tokens on them.
    """

    name: str = "title_length"
    is_soft: bool = False

    def evaluate(self, vacancy: Vacancy, profile: SearchProfile) -> RuleResult:  # noqa: ARG002
        stripped = (vacancy.title or "").strip()
        if len(stripped) < MIN_TITLE_LENGTH:
            return RuleResult(
                DECISION_REJECT,
                (f"vacancy title is too short: {len(stripped)} chars (minimum {MIN_TITLE_LENGTH})"),
            )
        return RuleResult(DECISION_ACCEPT)


# ---------------------------------------------------------------------------
# Aggregate factory
# ---------------------------------------------------------------------------


def default_rules() -> list:
    """Return the canonical ordered list of quick-filter rules.

    The order is preserved by the engine: reasons are concatenated in
    the same order the rules were registered, which keeps log output
    deterministic.
    """
    return [
        SalaryRangeRule(),
        LocationRule(),
        ScheduleRule(),
        KeywordRule(),
        TitleLengthRule(),
    ]


__all__ = [
    "KeywordRule",
    "LocationRule",
    "MIN_TITLE_LENGTH",
    "SALARY_SLACK",
    "SalaryRangeRule",
    "ScheduleRule",
    "TitleLengthRule",
    "default_rules",
]
