"""Failing tests for :class:`QuickFilterService`.

The service is a thin fan-out layer over :class:`QuickFilterEngine`:
it evaluates a batch of vacancies against one or many profiles and
returns the combined list of :class:`FilterDecision` values.

We inject a real engine with real rule instances — no mocks. The
service owns no business logic beyond the fan-out itself, but the
tests still verify it end-to-end so the wiring is checked.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence

import pytest

from apply_pilot.features.quick_filter.engine import QuickFilterEngine
from apply_pilot.features.quick_filter.models import (
    DECISION_ACCEPT,
    DECISION_REJECT,
    FilterDecision,
)
from apply_pilot.features.quick_filter.rules import (
    default_rules,
)
from apply_pilot.features.quick_filter.service import QuickFilterService
from apply_pilot.features.search_profiles.models import SearchProfile
from apply_pilot.features.sources.models import Vacancy

# ---------------------------------------------------------------------------
# Helpers / fixtures
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
    title: str = "Backend",
    keywords: str | None = None,
    salary_max: int | None = None,
    location: str | None = None,
    schedule: str | None = None,
) -> SearchProfile:
    p = SearchProfile(
        user_id=user_id,
        title=title,
        keywords=keywords,
        salary_max=salary_max,
        location=location,
        schedule=schedule,
        is_active=True,
    )
    p.id = uuid.uuid4()
    return p


@pytest.fixture
def engine() -> QuickFilterEngine:
    """A production engine with the default rule list."""
    return QuickFilterEngine(rules=default_rules())


@pytest.fixture
def service(engine: QuickFilterEngine) -> QuickFilterService:
    return QuickFilterService(engine=engine)


# ---------------------------------------------------------------------------
# Constructor / DI
# ---------------------------------------------------------------------------


class TestServiceConstruction:
    def test_stores_injected_engine(self, engine: QuickFilterEngine) -> None:
        service = QuickFilterService(engine=engine)

        assert service.engine is engine

    def test_engine_is_exposed_for_observability(self, service: QuickFilterService) -> None:
        """Downstream code (logging, metrics, future HTTP wiring) reads the
        engine off the service."""
        assert isinstance(service.engine, QuickFilterEngine)


# ---------------------------------------------------------------------------
# evaluate_for_profile
# ---------------------------------------------------------------------------


class TestEvaluateForProfile:
    def test_returns_one_decision_per_vacancy(self, service: QuickFilterService) -> None:
        vacancies = [_vacancy() for _ in range(3)]
        profile = _profile(uuid.uuid4(), keywords="python")

        decisions = service.evaluate_for_profile(vacancies, profile)

        assert len(decisions) == 3
        assert {d.vacancy_id for d in decisions} == {str(v.id) for v in vacancies}

    def test_all_decisions_carry_same_profile_id(self, service: QuickFilterService) -> None:
        vacancies = [_vacancy() for _ in range(2)]
        profile = _profile(uuid.uuid4())

        decisions = service.evaluate_for_profile(vacancies, profile)

        assert {d.profile_id for d in decisions} == {str(profile.id)}

    def test_returns_filter_decision_instances(self, service: QuickFilterService) -> None:
        decisions = service.evaluate_for_profile([_vacancy()], _profile(uuid.uuid4()))

        assert all(isinstance(d, FilterDecision) for d in decisions)

    def test_empty_vacancy_list_returns_empty(self, service: QuickFilterService) -> None:
        assert service.evaluate_for_profile([], _profile(uuid.uuid4())) == []

    def test_strict_mode_propagates_to_engine(self) -> None:
        """Verify the service forwards ``is_strict`` to the engine."""
        calls: list[bool] = []

        class _SpyEngine:
            def __init__(self) -> None:
                self._inner = QuickFilterEngine(rules=default_rules())

            def evaluate(self, vacancy, profile, *, is_strict=True):
                calls.append(is_strict)
                return self._inner.evaluate(vacancy, profile, is_strict=is_strict)

        service = QuickFilterService(engine=_SpyEngine())
        vacancies = [_vacancy(title="Sales Manager", description="B2B sales")]
        profile = _profile(uuid.uuid4(), keywords="python")

        # Default: strict.
        service.evaluate_for_profile(vacancies, profile)
        assert calls == [True]

        # Explicit non-strict: the soft keyword rule is bypassed.
        decisions = service.evaluate_for_profile(vacancies, profile, is_strict=False)
        assert calls == [True, False]
        assert all(d.decision == DECISION_ACCEPT for d in decisions)

    def test_uses_default_strict_when_not_specified(self, service: QuickFilterService) -> None:
        """Without an explicit ``is_strict`` the service defaults to strict."""
        vacancies = [_vacancy(title="Sales Manager", description="B2B sales")]
        profile = _profile(uuid.uuid4(), keywords="python")

        decisions = service.evaluate_for_profile(vacancies, profile)

        assert all(d.decision == DECISION_REJECT for d in decisions)

    def test_accepts_sequence_input(self, service: QuickFilterService) -> None:
        """The service accepts any ``Sequence[Vacancy]`` (tuple, list, etc.)."""
        vacancies: Sequence[Vacancy] = tuple(_vacancy() for _ in range(2))
        profile = _profile(uuid.uuid4())

        decisions = service.evaluate_for_profile(vacancies, profile)

        assert len(decisions) == 2


# ---------------------------------------------------------------------------
# evaluate_for_active_profiles
# ---------------------------------------------------------------------------


class TestEvaluateForActiveProfiles:
    def test_returns_one_decision_per_pair(self, service: QuickFilterService) -> None:
        vacancies = [_vacancy() for _ in range(2)]
        profiles = [_profile(uuid.uuid4(), title=f"P{i}") for i in range(3)]

        decisions = service.evaluate_for_active_profiles(vacancies, profiles)

        # 2 vacancies × 3 profiles = 6 decisions.
        assert len(decisions) == 6

    def test_pairing_includes_every_vacancy_for_every_profile(
        self, service: QuickFilterService
    ) -> None:
        v1 = _vacancy(title="Python Dev")
        v2 = _vacancy(title="Sales Manager", description="B2B sales")
        p1 = _profile(uuid.uuid4(), title="Py", keywords="python")
        p2 = _profile(uuid.uuid4(), title="Any")

        decisions = service.evaluate_for_active_profiles([v1, v2], [p1, p2])

        # Profile p1 wants python: v1 is accepted, v2 is rejected.
        by_pair = {(d.vacancy_id, d.profile_id): d for d in decisions}
        assert by_pair[(str(v1.id), str(p1.id))].decision == DECISION_ACCEPT
        assert by_pair[(str(v2.id), str(p1.id))].decision == DECISION_REJECT
        # Profile p2 has no keywords: both accepted.
        assert by_pair[(str(v1.id), str(p2.id))].decision == DECISION_ACCEPT
        assert by_pair[(str(v2.id), str(p2.id))].decision == DECISION_ACCEPT

    def test_empty_profiles_returns_empty(self, service: QuickFilterService) -> None:
        assert service.evaluate_for_active_profiles([_vacancy()], []) == []

    def test_empty_vacancies_returns_empty(self, service: QuickFilterService) -> None:
        assert service.evaluate_for_active_profiles([], [_profile(uuid.uuid4())]) == []

    def test_both_empty_returns_empty(self, service: QuickFilterService) -> None:
        assert service.evaluate_for_active_profiles([], []) == []

    def test_uses_strict_mode_by_default(self, service: QuickFilterService) -> None:
        """``evaluate_for_active_profiles`` has no ``is_strict`` override —
        it always runs in strict mode and defers non-strict usage to
        ``evaluate_for_profile``."""
        vacancies = [_vacancy(title="Sales Manager", description="B2B sales")]
        profile = _profile(uuid.uuid4(), keywords="python")

        decisions = service.evaluate_for_active_profiles(vacancies, [profile])

        assert all(d.decision == DECISION_REJECT for d in decisions)


# ---------------------------------------------------------------------------
# Order preservation
# ---------------------------------------------------------------------------


class TestOrderPreservation:
    def test_evaluate_for_profile_preserves_vacancy_order(
        self, service: QuickFilterService
    ) -> None:
        vacancies = [_vacancy() for _ in range(5)]
        profile = _profile(uuid.uuid4())

        decisions = service.evaluate_for_profile(vacancies, profile)

        assert [d.vacancy_id for d in decisions] == [str(v.id) for v in vacancies]

    def test_evaluate_for_active_profiles_preserves_input_order(
        self, service: QuickFilterService
    ) -> None:
        """Outer loop = profile order, inner loop = vacancy order, so the
        combined sequence is ``(p0, v0), (p0, v1), ..., (p1, v0), ...``."""
        vacancies = [_vacancy() for _ in range(2)]
        profiles = [_profile(uuid.uuid4(), title=f"P{i}") for i in range(2)]

        decisions = service.evaluate_for_active_profiles(vacancies, profiles)

        expected = [
            (str(vacancies[0].id), str(profiles[0].id)),
            (str(vacancies[1].id), str(profiles[0].id)),
            (str(vacancies[0].id), str(profiles[1].id)),
            (str(vacancies[1].id), str(profiles[1].id)),
        ]
        assert [(d.vacancy_id, d.profile_id) for d in decisions] == expected
