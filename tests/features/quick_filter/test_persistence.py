"""Service-level tests for ``QuickFilterService`` persistence wiring.

These tests exercise the new ``evaluate_and_persist_*`` methods on the
service, which run the in-memory engine and then write each
:class:`FilterDecision` to a repository. Tests use a real engine with
real rule instances and a real (in-memory) repository — no mocks, no
stubs beyond the fakes provided by the slice itself.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterator

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from apply_pilot.db import Base
from apply_pilot.features.quick_filter.engine import QuickFilterEngine
from apply_pilot.features.quick_filter.models import (
    DECISION_ACCEPT,
    DECISION_REJECT,
    FilterDecision,
)
from apply_pilot.features.quick_filter.persistence import (
    FilterDecisionRepository,
    FilterDecisionRow,
    InMemoryFilterDecisionRepository,
    SqlFilterDecisionRepository,
)
from apply_pilot.features.quick_filter.rules import default_rules
from apply_pilot.features.quick_filter.service import QuickFilterService
from apply_pilot.features.search_profiles.models import SearchProfile
from apply_pilot.features.sources.models import Vacancy

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
    return QuickFilterEngine(rules=default_rules())


# ---------------------------------------------------------------------------
# In-memory wiring
# ---------------------------------------------------------------------------


@pytest.fixture
def in_memory_repo() -> InMemoryFilterDecisionRepository:
    return InMemoryFilterDecisionRepository()


@pytest.fixture
def service(
    engine: QuickFilterEngine,
    in_memory_repo: InMemoryFilterDecisionRepository,
) -> QuickFilterService:
    return QuickFilterService(engine=engine, decision_repo=in_memory_repo)


class TestEvaluateAndPersistForProfile:
    def test_returns_persisted_rows(self, service: QuickFilterService) -> None:
        vacancies = [_vacancy() for _ in range(2)]
        profile = _profile(uuid.uuid4(), keywords="python")

        rows = service.evaluate_and_persist_for_profile(vacancies, profile)

        assert len(rows) == 2
        assert all(isinstance(r, FilterDecisionRow) for r in rows)

    def test_persists_to_repository(
        self,
        service: QuickFilterService,
        in_memory_repo: InMemoryFilterDecisionRepository,
    ) -> None:
        vacancies = [_vacancy() for _ in range(3)]
        profile = _profile(uuid.uuid4())

        service.evaluate_and_persist_for_profile(vacancies, profile)
        stored = in_memory_repo.list_by_profile(profile.id)

        assert len(stored) == 3

    def test_each_row_carries_same_profile_id(self, service: QuickFilterService) -> None:
        vacancies = [_vacancy() for _ in range(2)]
        profile = _profile(uuid.uuid4())

        rows = service.evaluate_and_persist_for_profile(vacancies, profile)

        assert {r.search_profile_id for r in rows} == {profile.id}

    def test_rejects_carry_json_encoded_reasons(self, service: QuickFilterService) -> None:
        """A vacancy with a too-high salary should be rejected; the
        reason must be encoded as a JSON list in the ``reasons`` column.

        The actual reason text is produced by :class:`SalaryRangeRule` —
        we only assert that *some* string is captured so the column
        round-trip works, without coupling the service to the rule's
        wording.
        """
        vacancy = _vacancy(salary_from=500_000)
        profile = _profile(uuid.uuid4(), salary_max=100_000)

        rows = service.evaluate_and_persist_for_profile([vacancy], profile)

        assert len(rows) == 1
        assert rows[0].decision == DECISION_REJECT
        decoded = json.loads(rows[0].reasons)
        assert isinstance(decoded, list)
        assert len(decoded) >= 1
        assert all(isinstance(r, str) for r in decoded)

    def test_accepts_carry_empty_reasons(self, service: QuickFilterService) -> None:
        """Accepted decisions must persist with an empty reasons list."""
        vacancy = _vacancy()
        profile = _profile(uuid.uuid4(), salary_max=1_000_000)

        rows = service.evaluate_and_persist_for_profile([vacancy], profile)

        assert len(rows) == 1
        assert rows[0].decision == DECISION_ACCEPT
        assert json.loads(rows[0].reasons) == []

    def test_rule_version_is_recorded(self, service: QuickFilterService) -> None:
        vacancy = _vacancy()
        profile = _profile(uuid.uuid4(), salary_max=1_000_000)

        rows = service.evaluate_and_persist_for_profile([vacancy], profile, rule_version=3)

        assert rows[0].rule_version == 3

    def test_default_rule_version_is_one(self, service: QuickFilterService) -> None:
        vacancy = _vacancy()
        profile = _profile(uuid.uuid4(), salary_max=1_000_000)

        rows = service.evaluate_and_persist_for_profile([vacancy], profile)

        assert rows[0].rule_version == 1

    def test_empty_vacancy_list_returns_empty(self, service: QuickFilterService) -> None:
        assert service.evaluate_and_persist_for_profile([], _profile(uuid.uuid4())) == []

    def test_evaluate_in_memory_and_persist_match(self, service: QuickFilterService) -> None:
        """The in-memory and persisted paths must produce the same decisions."""
        vacancies = [_vacancy() for _ in range(3)]
        profile = _profile(uuid.uuid4(), keywords="python")

        in_memory = service.evaluate_for_profile(vacancies, profile)
        persisted = service.evaluate_and_persist_for_profile(vacancies, profile)

        assert [d.decision for d in in_memory] == [r.decision for r in persisted]
        assert [d.reasons for d in in_memory] == [json.loads(r.reasons) for r in persisted]


class TestEvaluateAndPersistForActiveProfiles:
    def test_returns_count_persisted(self, service: QuickFilterService) -> None:
        vacancies = [_vacancy() for _ in range(2)]
        profiles = [_profile(uuid.uuid4(), title=f"P{i}") for i in range(3)]

        count = service.evaluate_and_persist_for_active_profiles(vacancies, profiles)

        # 2 vacancies × 3 profiles = 6 decisions persisted.
        assert count == 6

    def test_persists_for_each_profile(
        self,
        service: QuickFilterService,
        in_memory_repo: InMemoryFilterDecisionRepository,
    ) -> None:
        vacancies = [_vacancy() for _ in range(2)]
        profiles = [_profile(uuid.uuid4()) for _ in range(2)]

        service.evaluate_and_persist_for_active_profiles(vacancies, profiles)

        for profile in profiles:
            assert len(in_memory_repo.list_by_profile(profile.id)) == 2

    def test_empty_inputs_return_zero(self, service: QuickFilterService) -> None:
        assert service.evaluate_and_persist_for_active_profiles([], []) == 0
        assert service.evaluate_and_persist_for_active_profiles([], [_profile(uuid.uuid4())]) == 0
        assert service.evaluate_and_persist_for_active_profiles([_vacancy()], []) == 0


class TestRepositoryExposed:
    def test_repo_property_exposes_injected_repository(
        self,
        service: QuickFilterService,
        in_memory_repo: InMemoryFilterDecisionRepository,
    ) -> None:
        assert service.decision_repo is in_memory_repo

    def test_engine_property_still_works(self, service: QuickFilterService) -> None:
        assert isinstance(service.engine, QuickFilterEngine)


# ---------------------------------------------------------------------------
# Service still works without a repository (backward compat)
# ---------------------------------------------------------------------------


class TestBackwardCompat:
    def test_service_can_be_built_without_a_repo(self, engine: QuickFilterEngine) -> None:
        service = QuickFilterService(engine=engine)

        # In-memory methods still work; the new persist methods raise
        # when no repository is wired so a misconfiguration is loud.
        decisions = service.evaluate_for_profile(
            [_vacancy()], _profile(uuid.uuid4(), salary_max=1_000_000)
        )
        assert all(isinstance(d, FilterDecision) for d in decisions)

    def test_persist_raises_when_no_repository(self, engine: QuickFilterEngine) -> None:
        service = QuickFilterService(engine=engine)

        with pytest.raises(RuntimeError, match="decision_repo"):
            service.evaluate_and_persist_for_profile([_vacancy()], _profile(uuid.uuid4()))


# ---------------------------------------------------------------------------
# SQL repository end-to-end (a real database, a real service)
# ---------------------------------------------------------------------------


@pytest.fixture
def sql_session_factory() -> Iterator:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    try:
        yield Session
    finally:
        engine.dispose()


@pytest.fixture
def sql_repo(sql_session_factory) -> SqlFilterDecisionRepository:
    return SqlFilterDecisionRepository(session_factory=sql_session_factory)


def _seed(session_factory) -> tuple[uuid.UUID, uuid.UUID]:
    """Insert a User, SearchProfile, Vacancy and return (profile_id, vacancy_id)."""
    from apply_pilot.features.search_profiles.models import SearchProfile as SP
    from apply_pilot.features.sources.models import Vacancy as V
    from apply_pilot.features.users.models import User as U

    session = session_factory()
    try:
        user = U(
            id=uuid.uuid4(),
            email=f"u{uuid.uuid4().hex[:8]}@example.com",
            hashed_password="pwhash",
        )
        session.add(user)
        session.flush()
        profile = SP(
            id=uuid.uuid4(),
            user_id=user.id,
            title="Python",
            is_active=True,
        )
        vacancy = V(
            id=uuid.uuid4(),
            source="hh",
            source_id=str(uuid.uuid4()),
            title="Senior Python Developer",
            raw_data={},
        )
        session.add_all([profile, vacancy])
        session.commit()
        return profile.id, vacancy.id
    finally:
        session.close()


class TestServiceWithSqlRepository:
    def test_persists_through_sql_repository(
        self,
        engine: QuickFilterEngine,
        sql_repo: SqlFilterDecisionRepository,
        sql_session_factory,
    ) -> None:
        profile_id, vacancy_id = _seed(sql_session_factory)
        service = QuickFilterService(engine=engine, decision_repo=sql_repo)

        # Build a profile that matches the seeded one.
        profile = SearchProfile(
            id=profile_id,
            user_id=uuid.uuid4(),  # FK is the seeded user, but we don't need to re-read it
            title="Python",
            salary_max=1_000_000,
            is_active=True,
        )
        vacancy = Vacancy(
            id=vacancy_id,
            source="hh",
            source_id="svc-test",
            title="Senior Python Developer",
            salary_from=200_000,
            raw_data={},
        )

        rows = service.evaluate_and_persist_for_profile([vacancy], profile)

        assert len(rows) == 1
        assert rows[0].search_profile_id == profile_id
        assert rows[0].vacancy_id == vacancy_id
        assert json.loads(rows[0].reasons) == []

    def test_list_by_profile_finds_persisted_rows(
        self,
        engine: QuickFilterEngine,
        sql_repo: SqlFilterDecisionRepository,
        sql_session_factory,
    ) -> None:
        profile_id, vacancy_id = _seed(sql_session_factory)
        service = QuickFilterService(engine=engine, decision_repo=sql_repo)
        profile = SearchProfile(
            id=profile_id,
            user_id=uuid.uuid4(),
            title="Python",
            salary_max=1_000_000,
            is_active=True,
        )
        vacancy = Vacancy(
            id=vacancy_id,
            source="hh",
            source_id="svc-list",
            title="Senior Python Developer",
            salary_from=200_000,
            raw_data={},
        )

        service.evaluate_and_persist_for_profile([vacancy], profile)

        rows = list(sql_repo.list_by_profile(profile_id))
        assert len(rows) == 1
        assert rows[0].search_profile_id == profile_id

    def test_count_by_decision_after_persist(
        self,
        engine: QuickFilterEngine,
        sql_repo: SqlFilterDecisionRepository,
        sql_session_factory,
    ) -> None:
        profile_id, vacancy_id = _seed(sql_session_factory)
        service = QuickFilterService(engine=engine, decision_repo=sql_repo)
        profile = SearchProfile(
            id=profile_id,
            user_id=uuid.uuid4(),
            title="Python",
            salary_max=1_000_000,
            is_active=True,
        )
        accept_vacancy = Vacancy(
            id=vacancy_id,
            source="hh",
            source_id="svc-accept",
            title="Senior Python Developer",
            salary_from=200_000,
            raw_data={},
        )

        service.evaluate_and_persist_for_profile([accept_vacancy], profile)
        counts = sql_repo.count_by_decision(profile_id)
        assert counts == {DECISION_ACCEPT: 1}


def test_repository_protocol_accepts_in_memory() -> None:
    """The Protocol can be satisfied by the in-memory fake."""
    repo: FilterDecisionRepository = InMemoryFilterDecisionRepository()
    assert isinstance(repo, FilterDecisionRepository)
