"""Failing tests for the five quick-filter rules.

Each rule is exercised in isolation against freshly-built
:class:`Vacancy` and :class:`SearchProfile` ORM instances — the rules
read the existing model fields and never mutate them, so the only
state we need is the constructor arguments.
"""

from __future__ import annotations

import uuid

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
    description: str | None = "We are looking for a Python developer with Django experience.",
    salary_from: int | None = 200_000,
    salary_to: int | None = 300_000,
    location: str | None = "Москва",
    schedule: str | None = "fullDay",
) -> Vacancy:
    """Build a fully-populated :class:`Vacancy` mirroring a normalised import."""
    v = Vacancy(
        source="hh",
        source_id=str(uuid.uuid4()),
        title=title,
        description=description,
        salary_from=salary_from,
        salary_to=salary_to,
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
    salary_min: int | None = None,
    salary_max: int | None = None,
    location: str | None = None,
    schedule: str | None = None,
) -> SearchProfile:
    """Build a :class:`SearchProfile` owned by ``user_id``."""
    p = SearchProfile(
        user_id=user_id,
        title="Backend",
        keywords=keywords,
        salary_min=salary_min,
        salary_max=salary_max,
        location=location,
        schedule=schedule,
        is_active=True,
    )
    p.id = uuid.uuid4()
    return p


# ---------------------------------------------------------------------------
# SalaryRangeRule
# ---------------------------------------------------------------------------


class TestSalaryRangeRule:
    def setup_method(self) -> None:
        self.rule = SalaryRangeRule()

    def test_accepts_when_salary_from_within_max_with_slack(self) -> None:
        """salary_from at exactly max*1.1 is allowed (10% slack)."""
        vacancy = _vacancy(salary_from=110_000)
        profile = _profile(uuid.uuid4(), salary_max=100_000)

        result = self.rule.evaluate(vacancy, profile)

        assert result.decision == "accept"
        assert result.reason is None

    def test_rejects_when_salary_from_exceeds_max_with_slack(self) -> None:
        """salary_from > max*1.1 means the profile's max is too low."""
        vacancy = _vacancy(salary_from=120_000)
        profile = _profile(uuid.uuid4(), salary_max=100_000)

        result = self.rule.evaluate(vacancy, profile)

        assert result.decision == "reject"
        assert "salary" in (result.reason or "").lower()

    def test_accepts_when_salary_from_just_below_max(self) -> None:
        """salary_from below profile's max must accept."""
        vacancy = _vacancy(salary_from=90_000)
        profile = _profile(uuid.uuid4(), salary_max=100_000)

        assert self.rule.evaluate(vacancy, profile).decision == "accept"

    def test_accepts_when_vacancy_salary_from_missing(self) -> None:
        """Missing vacancy salary is not grounds for rejection (the vacancy may
        be open to discussion)."""
        vacancy = _vacancy(salary_from=None)
        profile = _profile(uuid.uuid4(), salary_max=100_000)

        assert self.rule.evaluate(vacancy, profile).decision == "accept"

    def test_accepts_when_profile_salary_max_missing(self) -> None:
        """Missing profile ceiling is not grounds for rejection (the user has
        no salary cap)."""
        vacancy = _vacancy(salary_from=500_000)
        profile = _profile(uuid.uuid4(), salary_max=None)

        assert self.rule.evaluate(vacancy, profile).decision == "accept"

    def test_accepts_when_both_missing(self) -> None:
        """Both missing is accept (no data to compare)."""
        vacancy = _vacancy(salary_from=None)
        profile = _profile(uuid.uuid4(), salary_max=None)

        assert self.rule.evaluate(vacancy, profile).decision == "accept"


# ---------------------------------------------------------------------------
# LocationRule
# ---------------------------------------------------------------------------


class TestLocationRule:
    def setup_method(self) -> None:
        self.rule = LocationRule()

    def test_accepts_when_profile_location_missing(self) -> None:
        """A profile with no location preference cannot reject on location."""
        vacancy = _vacancy(location="Москва")
        profile = _profile(uuid.uuid4(), location=None)

        assert self.rule.evaluate(vacancy, profile).decision == "accept"

    def test_accepts_when_substring_match(self) -> None:
        """Vacancy location containing profile location is accept (substring)."""
        vacancy = _vacancy(location="Москва, ул. Тверская")
        profile = _profile(uuid.uuid4(), location="Москва")

        assert self.rule.evaluate(vacancy, profile).decision == "accept"

    def test_accepts_case_insensitive_match(self) -> None:
        """Case-insensitive substring match is accept."""
        vacancy = _vacancy(location="Saint Petersburg")
        profile = _profile(uuid.uuid4(), location="saint")

        assert self.rule.evaluate(vacancy, profile).decision == "accept"

    def test_rejects_when_no_match(self) -> None:
        """Vacancy location not containing profile location is reject."""
        vacancy = _vacancy(location="Санкт-Петербург")
        profile = _profile(uuid.uuid4(), location="Москва")

        result = self.rule.evaluate(vacancy, profile)

        assert result.decision == "reject"
        assert "location" in (result.reason or "").lower()

    def test_rejects_when_vacancy_location_missing(self) -> None:
        """A vacancy with no location cannot satisfy a profile's location."""
        vacancy = _vacancy(location=None)
        profile = _profile(uuid.uuid4(), location="Москва")

        assert self.rule.evaluate(vacancy, profile).decision == "reject"

    def test_rejects_with_empty_profile_location(self) -> None:
        """Empty profile location is treated as 'no preference' — accept."""
        vacancy = _vacancy(location="Москва")
        profile = _profile(uuid.uuid4(), location="")

        assert self.rule.evaluate(vacancy, profile).decision == "accept"


# ---------------------------------------------------------------------------
# ScheduleRule
# ---------------------------------------------------------------------------


class TestScheduleRule:
    def setup_method(self) -> None:
        self.rule = ScheduleRule()

    def test_accepts_when_profile_schedule_missing(self) -> None:
        """A profile with no schedule preference cannot reject on schedule."""
        vacancy = _vacancy(schedule="remote")
        profile = _profile(uuid.uuid4(), schedule=None)

        assert self.rule.evaluate(vacancy, profile).decision == "accept"

    def test_accepts_when_schedules_match(self) -> None:
        vacancy = _vacancy(schedule="fullDay")
        profile = _profile(uuid.uuid4(), schedule="fullDay")

        assert self.rule.evaluate(vacancy, profile).decision == "accept"

    def test_rejects_when_schedules_differ(self) -> None:
        vacancy = _vacancy(schedule="remote")
        profile = _profile(uuid.uuid4(), schedule="fullDay")

        result = self.rule.evaluate(vacancy, profile)

        assert result.decision == "reject"
        assert "schedule" in (result.reason or "").lower()

    def test_rejects_when_vacancy_schedule_missing(self) -> None:
        """A vacancy with no schedule cannot satisfy a profile's schedule."""
        vacancy = _vacancy(schedule=None)
        profile = _profile(uuid.uuid4(), schedule="fullDay")

        assert self.rule.evaluate(vacancy, profile).decision == "reject"


# ---------------------------------------------------------------------------
# KeywordRule
# ---------------------------------------------------------------------------


class TestKeywordRule:
    def setup_method(self) -> None:
        self.rule = KeywordRule()

    def test_neutral_when_profile_has_no_keywords(self) -> None:
        """No keywords configured means the rule has no opinion."""
        vacancy = _vacancy()
        profile = _profile(uuid.uuid4(), keywords=None)

        result = self.rule.evaluate(vacancy, profile)

        assert result.decision == "neutral"
        assert "keyword" in (result.reason or "").lower()

    def test_neutral_when_profile_has_empty_string_keywords(self) -> None:
        """An empty string is treated like None."""
        vacancy = _vacancy()
        profile = _profile(uuid.uuid4(), keywords="")

        assert self.rule.evaluate(vacancy, profile).decision == "neutral"

    def test_accepts_when_keyword_matches_title(self) -> None:
        """A keyword found in the title (case-insensitive) is a match."""
        vacancy = _vacancy(title="Senior Python Developer")
        profile = _profile(uuid.uuid4(), keywords="python")

        assert self.rule.evaluate(vacancy, profile).decision == "accept"

    def test_accepts_when_keyword_matches_description(self) -> None:
        """A keyword found in the description is a match."""
        vacancy = _vacancy(description="Django, FastAPI, PostgreSQL")
        profile = _profile(uuid.uuid4(), keywords="fastapi")

        assert self.rule.evaluate(vacancy, profile).decision == "accept"

    def test_rejects_when_no_keyword_matches(self) -> None:
        """If no keyword is found in title or description, reject."""
        vacancy = _vacancy(title="Sales Manager", description="B2B sales experience required")
        profile = _profile(uuid.uuid4(), keywords="python, django")

        result = self.rule.evaluate(vacancy, profile)

        assert result.decision == "reject"
        assert "keyword" in (result.reason or "").lower()

    def test_handles_multiple_keywords_comma_separated(self) -> None:
        """A profile keyword list is comma-separated; any one matching is enough."""
        vacancy = _vacancy(title="Java Backend Developer")
        profile = _profile(uuid.uuid4(), keywords="python, java, go")

        assert self.rule.evaluate(vacancy, profile).decision == "accept"

    def test_keyword_rule_is_marked_soft(self) -> None:
        """The engine relies on this flag to bypass the rule in non-strict mode."""
        assert KeywordRule.is_soft is True


# ---------------------------------------------------------------------------
# TitleLengthRule
# ---------------------------------------------------------------------------


class TestTitleLengthRule:
    def setup_method(self) -> None:
        self.rule = TitleLengthRule()

    def test_rejects_title_shorter_than_three_chars(self) -> None:
        vacancy = _vacancy(title="ab")
        profile = _profile(uuid.uuid4())

        result = self.rule.evaluate(vacancy, profile)

        assert result.decision == "reject"
        assert "title" in (result.reason or "").lower()

    def test_rejects_title_with_only_whitespace(self) -> None:
        """Stripping before measuring means all-whitespace is too short."""
        vacancy = _vacancy(title="   ")
        profile = _profile(uuid.uuid4())

        assert self.rule.evaluate(vacancy, profile).decision == "reject"

    def test_rejects_empty_title(self) -> None:
        vacancy = _vacancy(title="")
        profile = _profile(uuid.uuid4())

        assert self.rule.evaluate(vacancy, profile).decision == "reject"

    def test_accepts_title_exactly_three_chars(self) -> None:
        """Boundary: length 3 is acceptable."""
        vacancy = _vacancy(title="Dev")
        profile = _profile(uuid.uuid4())

        assert self.rule.evaluate(vacancy, profile).decision == "accept"

    def test_accepts_long_title_with_surrounding_whitespace(self) -> None:
        """Whitespace around a long title is fine; the strip handles it."""
        vacancy = _vacancy(title="  Python Developer  ")
        profile = _profile(uuid.uuid4())

        assert self.rule.evaluate(vacancy, profile).decision == "accept"

    def test_hard_rules_are_not_soft(self) -> None:
        """The hard rules must not be marked soft — the engine uses this flag
        to decide which rules to bypass in non-strict mode."""
        assert SalaryRangeRule.is_soft is False
        assert LocationRule.is_soft is False
        assert ScheduleRule.is_soft is False
        assert TitleLengthRule.is_soft is False


# ---------------------------------------------------------------------------
# Protocol surface
# ---------------------------------------------------------------------------


class TestFilterRuleProtocol:
    def test_all_rules_expose_name_and_evaluate(self) -> None:
        """Every rule exposes a stable ``name`` and an ``evaluate`` callable.

        The engine depends on the duck-typed surface rather than a concrete
        base class; this test is the safety net.
        """
        rules = [
            SalaryRangeRule(),
            LocationRule(),
            ScheduleRule(),
            KeywordRule(),
            TitleLengthRule(),
        ]
        for rule in rules:
            assert isinstance(rule.name, str) and rule.name
            assert callable(getattr(rule, "evaluate", None))
