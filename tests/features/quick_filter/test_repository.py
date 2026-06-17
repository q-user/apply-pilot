"""Tests for the ``FilterDecisionRepository`` implementations.

* :class:`InMemoryFilterDecisionRepository` is exercised directly so the
  dict-backed fake's contract is verified.
* :class:`SqlFilterDecisionRepository` is exercised end-to-end against a
  fresh in-memory sqlite engine so the SQL queries, JSON encoding of the
  ``reasons`` column, and the unique constraints are covered too.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterator

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from apply_pilot.db import Base
from apply_pilot.features.quick_filter.models import (
    DECISION_ACCEPT,
    DECISION_REJECT,
)
from apply_pilot.features.quick_filter.persistence import (
    FilterDecisionRepository,
    FilterDecisionRow,
    InMemoryFilterDecisionRepository,
    SqlFilterDecisionRepository,
)
from apply_pilot.features.search_profiles import models as _sp_models  # noqa: F401
from apply_pilot.features.search_profiles.models import SearchProfile
from apply_pilot.features.sources import models as _sources_models  # noqa: F401
from apply_pilot.features.sources.models import Vacancy
from apply_pilot.features.users import models as _users_models  # noqa: F401
from apply_pilot.features.users.models import User

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _row(
    *,
    profile_id: uuid.UUID | None = None,
    vacancy_id: uuid.UUID | None = None,
    decision: str = DECISION_ACCEPT,
    reasons: list[str] | None = None,
    rule_version: int = 1,
) -> FilterDecisionRow:
    """Build a ``FilterDecisionRow`` with reasonable defaults."""
    return FilterDecisionRow(
        id=uuid.uuid4(),
        search_profile_id=profile_id or uuid.uuid4(),
        vacancy_id=vacancy_id or uuid.uuid4(),
        decision=decision,
        reasons=json.dumps(reasons or [], ensure_ascii=False),
        rule_version=rule_version,
    )


def _seed_user(session, *, email: str | None = None) -> User:
    """Insert a minimal :class:`User` row that satisfies the FK chain.

    ``hashed_password`` is ``NOT NULL`` so the seed must provide a
    placeholder value. Returns the inserted (and flushed) user.
    """
    user = User(
        id=uuid.uuid4(),
        email=email or f"u{uuid.uuid4().hex[:8]}@example.com",
        hashed_password="pwhash",
    )
    session.add(user)
    session.flush()
    return user


# ---------------------------------------------------------------------------
# Protocol structural check
# ---------------------------------------------------------------------------


def test_in_memory_repo_satisfies_protocol() -> None:
    """The in-memory fake must be usable wherever the Protocol is expected."""
    repo: FilterDecisionRepository = InMemoryFilterDecisionRepository()
    assert isinstance(repo, FilterDecisionRepository)


# ---------------------------------------------------------------------------
# In-memory repository
# ---------------------------------------------------------------------------


@pytest.fixture
def in_memory_repo() -> InMemoryFilterDecisionRepository:
    return InMemoryFilterDecisionRepository()


class TestCreate:
    def test_create_assigns_id_and_timestamp(
        self, in_memory_repo: InMemoryFilterDecisionRepository
    ) -> None:
        row = _row()

        created = in_memory_repo.create(row)

        assert created.id is not None
        assert created.created_at is not None

    def test_create_stores_in_primary_index(
        self, in_memory_repo: InMemoryFilterDecisionRepository
    ) -> None:
        row = _row()

        in_memory_repo.create(row)

        assert in_memory_repo.get_by_id(row.id) is row

    def test_create_with_unspecified_id_gets_a_uuid(
        self, in_memory_repo: InMemoryFilterDecisionRepository
    ) -> None:
        row = FilterDecisionRow(
            search_profile_id=uuid.uuid4(),
            vacancy_id=uuid.uuid4(),
            decision=DECISION_ACCEPT,
            reasons="[]",
        )

        created = in_memory_repo.create(row)

        assert created.id is not None
        assert isinstance(created.id, uuid.UUID)


class TestGetById:
    def test_get_by_id_returns_row(self, in_memory_repo: InMemoryFilterDecisionRepository) -> None:
        row = _row()
        in_memory_repo.create(row)

        assert in_memory_repo.get_by_id(row.id) is row

    def test_get_by_id_returns_none_for_unknown(
        self, in_memory_repo: InMemoryFilterDecisionRepository
    ) -> None:
        assert in_memory_repo.get_by_id(uuid.uuid4()) is None


class TestListByProfile:
    def test_returns_only_matching_profile(
        self, in_memory_repo: InMemoryFilterDecisionRepository
    ) -> None:
        p1, p2 = uuid.uuid4(), uuid.uuid4()
        for _ in range(2):
            in_memory_repo.create(_row(profile_id=p1))
        in_memory_repo.create(_row(profile_id=p2))

        assert len(list(in_memory_repo.list_by_profile(p1))) == 2
        assert len(list(in_memory_repo.list_by_profile(p2))) == 1

    def test_filters_by_decision(self, in_memory_repo: InMemoryFilterDecisionRepository) -> None:
        profile_id = uuid.uuid4()
        accepted = _row(profile_id=profile_id, decision=DECISION_ACCEPT)
        rejected = _row(
            profile_id=profile_id,
            decision=DECISION_REJECT,
            reasons=["bad_location"],
        )
        in_memory_repo.create(accepted)
        in_memory_repo.create(rejected)

        accepted_rows = list(in_memory_repo.list_by_profile(profile_id, decision=DECISION_ACCEPT))
        rejected_rows = list(in_memory_repo.list_by_profile(profile_id, decision=DECISION_REJECT))

        assert [r.id for r in accepted_rows] == [accepted.id]
        assert [r.id for r in rejected_rows] == [rejected.id]

    def test_respects_limit(self, in_memory_repo: InMemoryFilterDecisionRepository) -> None:
        profile_id = uuid.uuid4()
        for _ in range(5):
            in_memory_repo.create(_row(profile_id=profile_id))

        assert len(list(in_memory_repo.list_by_profile(profile_id, limit=3))) == 3

    def test_orders_newest_first(self, in_memory_repo: InMemoryFilterDecisionRepository) -> None:
        profile_id = uuid.uuid4()
        first = _row(profile_id=profile_id)
        in_memory_repo.create(first)
        second = _row(profile_id=profile_id)
        in_memory_repo.create(second)
        third = _row(profile_id=profile_id)
        in_memory_repo.create(third)

        ids = [r.id for r in in_memory_repo.list_by_profile(profile_id)]

        # Strict ordering: third > second > first.
        assert ids == [third.id, second.id, first.id]


class TestListByVacancy:
    def test_returns_only_matching_vacancy(
        self, in_memory_repo: InMemoryFilterDecisionRepository
    ) -> None:
        v1, v2 = uuid.uuid4(), uuid.uuid4()
        in_memory_repo.create(_row(vacancy_id=v1))
        in_memory_repo.create(_row(vacancy_id=v1))
        in_memory_repo.create(_row(vacancy_id=v2))

        assert len(list(in_memory_repo.list_by_vacancy(v1))) == 2
        assert len(list(in_memory_repo.list_by_vacancy(v2))) == 1

    def test_returns_empty_for_unknown(
        self, in_memory_repo: InMemoryFilterDecisionRepository
    ) -> None:
        assert list(in_memory_repo.list_by_vacancy(uuid.uuid4())) == []


class TestCountByDecision:
    def test_counts_each_decision(self, in_memory_repo: InMemoryFilterDecisionRepository) -> None:
        profile_id = uuid.uuid4()
        for _ in range(3):
            in_memory_repo.create(_row(profile_id=profile_id, decision=DECISION_ACCEPT))
        for _ in range(2):
            in_memory_repo.create(
                _row(
                    profile_id=profile_id,
                    decision=DECISION_REJECT,
                    reasons=["bad"],
                )
            )

        counts = in_memory_repo.count_by_decision(profile_id)

        assert counts == {DECISION_ACCEPT: 3, DECISION_REJECT: 2}

    def test_returns_empty_for_profile_with_no_rows(
        self, in_memory_repo: InMemoryFilterDecisionRepository
    ) -> None:
        assert in_memory_repo.count_by_decision(uuid.uuid4()) == {}


# ---------------------------------------------------------------------------
# SQL repository
# ---------------------------------------------------------------------------


@pytest.fixture
def sql_session_factory() -> Iterator:
    """Yield a ``sessionmaker`` bound to a fresh in-memory sqlite engine.

    The FK chain through ``search_profiles.user_id`` requires the
    ``users`` table to exist; we bootstrap with the full metadata so
    every imported model is created.
    """
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


def _seed_profile_and_vacancy(session_factory) -> tuple[uuid.UUID, uuid.UUID]:
    """Insert a User, a SearchProfile, and a Vacancy, returning the latter two's ids."""
    session = session_factory()
    try:
        user = _seed_user(session)
        profile = SearchProfile(
            id=uuid.uuid4(),
            user_id=user.id,
            title="Python",
            is_active=True,
        )
        vacancy = Vacancy(
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


class TestSqlRepository:
    def test_create_persists_row(
        self, sql_repo: SqlFilterDecisionRepository, sql_session_factory
    ) -> None:
        profile_id, vacancy_id = _seed_profile_and_vacancy(sql_session_factory)
        row = _row(
            profile_id=profile_id,
            vacancy_id=vacancy_id,
            decision=DECISION_REJECT,
            reasons=["salary_too_high", "wrong_location"],
        )

        created = sql_repo.create(row)

        assert created.id is not None
        assert created.created_at is not None
        assert sql_repo.get_by_id(created.id) is not None

    def test_reasons_round_trip_via_json(
        self, sql_repo: SqlFilterDecisionRepository, sql_session_factory
    ) -> None:
        profile_id, vacancy_id = _seed_profile_and_vacancy(sql_session_factory)
        original = ["salary_too_high", "wrong_location"]
        row = _row(
            profile_id=profile_id,
            vacancy_id=vacancy_id,
            decision=DECISION_REJECT,
            reasons=original,
        )

        created = sql_repo.create(row)
        loaded = sql_repo.get_by_id(created.id)

        assert loaded is not None
        assert json.loads(loaded.reasons) == original

    def test_list_by_profile_returns_only_matching(
        self, sql_repo: SqlFilterDecisionRepository, sql_session_factory
    ) -> None:
        p1, p2 = uuid.uuid4(), uuid.uuid4()
        session = sql_session_factory()
        try:
            user = _seed_user(session, email="multi@example.com")
            session.add(SearchProfile(id=p1, user_id=user.id, title="A", is_active=True))
            session.add(SearchProfile(id=p2, user_id=user.id, title="B", is_active=True))
            v1 = Vacancy(id=uuid.uuid4(), source="hh", source_id="1", title="x", raw_data={})
            v2 = Vacancy(id=uuid.uuid4(), source="hh", source_id="2", title="y", raw_data={})
            v3 = Vacancy(id=uuid.uuid4(), source="hh", source_id="3", title="z", raw_data={})
            session.add_all([v1, v2, v3])
            session.flush()
            session.add_all(
                [
                    _row(profile_id=p1, vacancy_id=v1.id),
                    _row(profile_id=p1, vacancy_id=v2.id),
                    _row(profile_id=p2, vacancy_id=v3.id),
                ]
            )
            session.commit()
        finally:
            session.close()

        assert len(list(sql_repo.list_by_profile(p1))) == 2
        assert len(list(sql_repo.list_by_profile(p2))) == 1

    def test_list_by_profile_filters_by_decision(
        self, sql_repo: SqlFilterDecisionRepository, sql_session_factory
    ) -> None:
        profile_id, vacancy_id = _seed_profile_and_vacancy(sql_session_factory)
        session = sql_session_factory()
        try:
            session.add_all(
                [
                    _row(
                        profile_id=profile_id,
                        vacancy_id=vacancy_id,
                        decision=DECISION_ACCEPT,
                    ),
                    _row(
                        profile_id=profile_id,
                        vacancy_id=vacancy_id,
                        decision=DECISION_REJECT,
                        reasons=["bad"],
                    ),
                ]
            )
            session.commit()
        finally:
            session.close()

        accepted = list(sql_repo.list_by_profile(profile_id, decision=DECISION_ACCEPT))
        rejected = list(sql_repo.list_by_profile(profile_id, decision=DECISION_REJECT))

        assert len(accepted) == 1
        assert accepted[0].decision == DECISION_ACCEPT
        assert len(rejected) == 1
        assert rejected[0].decision == DECISION_REJECT

    def test_list_by_profile_respects_limit(
        self, sql_repo: SqlFilterDecisionRepository, sql_session_factory
    ) -> None:
        profile_id, vacancy_id = _seed_profile_and_vacancy(sql_session_factory)
        session = sql_session_factory()
        try:
            session.add_all([_row(profile_id=profile_id, vacancy_id=vacancy_id) for _ in range(5)])
            session.commit()
        finally:
            session.close()

        assert len(list(sql_repo.list_by_profile(profile_id, limit=3))) == 3

    def test_list_by_vacancy(
        self, sql_repo: SqlFilterDecisionRepository, sql_session_factory
    ) -> None:
        profile_id, vacancy_id = _seed_profile_and_vacancy(sql_session_factory)
        v2 = Vacancy(
            id=uuid.uuid4(),
            source="hh",
            source_id="v2",
            title="x",
            raw_data={},
        )
        session = sql_session_factory()
        try:
            session.add(v2)
            session.flush()
            v2_id = v2.id
            session.add_all(
                [
                    _row(profile_id=profile_id, vacancy_id=vacancy_id),
                    _row(profile_id=profile_id, vacancy_id=vacancy_id),
                    _row(profile_id=profile_id, vacancy_id=v2_id),
                ]
            )
            session.commit()
        finally:
            session.close()

        assert len(list(sql_repo.list_by_vacancy(vacancy_id))) == 2
        assert len(list(sql_repo.list_by_vacancy(v2_id))) == 1

    def test_count_by_decision(
        self, sql_repo: SqlFilterDecisionRepository, sql_session_factory
    ) -> None:
        profile_id, vacancy_id = _seed_profile_and_vacancy(sql_session_factory)
        session = sql_session_factory()
        try:
            session.add_all(
                [
                    _row(
                        profile_id=profile_id,
                        vacancy_id=vacancy_id,
                        decision=DECISION_ACCEPT,
                    )
                    for _ in range(3)
                ]
            )
            session.add_all(
                [
                    _row(
                        profile_id=profile_id,
                        vacancy_id=vacancy_id,
                        decision=DECISION_REJECT,
                        reasons=["bad"],
                    )
                    for _ in range(2)
                ]
            )
            session.commit()
        finally:
            session.close()

        counts = sql_repo.count_by_decision(profile_id)

        assert counts == {DECISION_ACCEPT: 3, DECISION_REJECT: 2}

    def test_repository_without_factory_raises(self) -> None:
        repo = SqlFilterDecisionRepository()
        with pytest.raises(RuntimeError, match="not bound"):
            repo.get_by_id(uuid.uuid4())
