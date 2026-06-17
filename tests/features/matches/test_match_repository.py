"""Tests for the VacancyMatchRepository implementations.

* :class:`InMemoryVacancyMatchRepository` is exercised directly so the
  dict-backed fake's contract is verified.
* :class:`SqlVacancyMatchRepository` is exercised end-to-end against a
  fresh in-memory sqlite engine so the SQL upsert and JOIN-based
  ``list_by_user`` are covered too.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from apply_pilot.db import Base
from apply_pilot.features.matches import models as _matches_models  # noqa: F401
from apply_pilot.features.matches.models import MatchStatus, VacancyMatch
from apply_pilot.features.matches.repository import (
    InMemoryVacancyMatchRepository,
    SqlVacancyMatchRepository,
)
from apply_pilot.features.search_profiles import models as _sp_models  # noqa: F401
from apply_pilot.features.search_profiles.models import SearchProfile
from apply_pilot.features.search_profiles.repository import InMemorySearchProfileRepository
from apply_pilot.features.sources import models as _sources_models  # noqa: F401
from apply_pilot.features.sources.models import Vacancy
from apply_pilot.features.users import models as _users_models  # noqa: F401
from apply_pilot.shared.errors import NotFoundError

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _vacancy(source_id: str = "hh-1", title: str = "Python Dev") -> Vacancy:
    v = Vacancy(
        source="hh",
        source_id=source_id,
        title=title,
        raw_data={"id": source_id, "name": title},
    )
    v.id = uuid.uuid4()
    return v


def _profile(user_id: uuid.UUID, *, is_active: bool = True) -> SearchProfile:
    p = SearchProfile(user_id=user_id, title="Python", is_active=is_active)
    p.id = uuid.uuid4()
    return p


# ---------------------------------------------------------------------------
# In-memory repository
# ---------------------------------------------------------------------------


@pytest.fixture
def profile_repo() -> InMemorySearchProfileRepository:
    return InMemorySearchProfileRepository()


@pytest.fixture
def match_repo(profile_repo: InMemorySearchProfileRepository) -> InMemoryVacancyMatchRepository:
    return InMemoryVacancyMatchRepository(list_user_profiles=profile_repo.list_by_user)


class TestCreate:
    def test_create_assigns_id_and_timestamps(
        self, match_repo: InMemoryVacancyMatchRepository
    ) -> None:
        profile_id = uuid.uuid4()
        vacancy_id = uuid.uuid4()
        match = VacancyMatch(search_profile_id=profile_id, vacancy_id=vacancy_id)

        created = match_repo.create(match)

        assert created.id is not None
        assert created.status == MatchStatus.NEW.value
        assert created.created_at is not None
        assert created.updated_at is not None

    def test_create_stores_in_primary_index(
        self, match_repo: InMemoryVacancyMatchRepository
    ) -> None:
        match = VacancyMatch(search_profile_id=uuid.uuid4(), vacancy_id=uuid.uuid4())
        match_repo.create(match)

        assert match_repo.get_by_id(match.id) is match


class TestGetById:
    def test_get_by_id_returns_match(self, match_repo: InMemoryVacancyMatchRepository) -> None:
        match = VacancyMatch(search_profile_id=uuid.uuid4(), vacancy_id=uuid.uuid4())
        match_repo.create(match)

        assert match_repo.get_by_id(match.id) is match

    def test_get_by_id_returns_none_for_unknown(
        self, match_repo: InMemoryVacancyMatchRepository
    ) -> None:
        assert match_repo.get_by_id(uuid.uuid4()) is None


class TestListByProfile:
    def test_returns_only_matching_profile(
        self, match_repo: InMemoryVacancyMatchRepository
    ) -> None:
        p1, p2 = uuid.uuid4(), uuid.uuid4()
        for _ in range(2):
            match_repo.create(VacancyMatch(search_profile_id=p1, vacancy_id=uuid.uuid4()))
        match_repo.create(VacancyMatch(search_profile_id=p2, vacancy_id=uuid.uuid4()))

        assert len(list(match_repo.list_by_profile(p1))) == 2
        assert len(list(match_repo.list_by_profile(p2))) == 1

    def test_filters_by_status(self, match_repo: InMemoryVacancyMatchRepository) -> None:
        profile_id = uuid.uuid4()
        m1 = VacancyMatch(search_profile_id=profile_id, vacancy_id=uuid.uuid4())
        m1.status = MatchStatus.NEW.value
        match_repo.create(m1)
        m2 = VacancyMatch(search_profile_id=profile_id, vacancy_id=uuid.uuid4())
        m2.status = MatchStatus.ACCEPTED.value
        match_repo.create(m2)

        new_only = list(match_repo.list_by_profile(profile_id, status=MatchStatus.NEW.value))
        accepted_only = list(
            match_repo.list_by_profile(profile_id, status=MatchStatus.ACCEPTED.value)
        )

        assert [m.id for m in new_only] == [m1.id]
        assert [m.id for m in accepted_only] == [m2.id]

    def test_respects_limit(self, match_repo: InMemoryVacancyMatchRepository) -> None:
        profile_id = uuid.uuid4()
        for _ in range(5):
            match_repo.create(VacancyMatch(search_profile_id=profile_id, vacancy_id=uuid.uuid4()))

        assert len(list(match_repo.list_by_profile(profile_id, limit=3))) == 3


class TestListByUser:
    def test_returns_only_own_matches(
        self,
        match_repo: InMemoryVacancyMatchRepository,
        profile_repo: InMemorySearchProfileRepository,
    ) -> None:
        user_id = uuid.uuid4()
        other_id = uuid.uuid4()
        mine = _profile(user_id)
        theirs = _profile(other_id)
        profile_repo.create(mine)
        profile_repo.create(theirs)
        match_repo.create(VacancyMatch(search_profile_id=mine.id, vacancy_id=uuid.uuid4()))
        match_repo.create(VacancyMatch(search_profile_id=theirs.id, vacancy_id=uuid.uuid4()))

        mine_listed = list(match_repo.list_by_user(user_id))
        theirs_listed = list(match_repo.list_by_user(other_id))

        assert len(mine_listed) == 1
        assert mine_listed[0].search_profile_id == mine.id
        assert len(theirs_listed) == 1
        assert theirs_listed[0].search_profile_id == theirs.id

    def test_filters_by_status(
        self,
        match_repo: InMemoryVacancyMatchRepository,
        profile_repo: InMemorySearchProfileRepository,
    ) -> None:
        user_id = uuid.uuid4()
        profile = _profile(user_id)
        profile_repo.create(profile)
        m1 = VacancyMatch(search_profile_id=profile.id, vacancy_id=uuid.uuid4())
        m1.status = MatchStatus.NEW.value
        m2 = VacancyMatch(search_profile_id=profile.id, vacancy_id=uuid.uuid4())
        m2.status = MatchStatus.ACCEPTED.value
        match_repo.create(m1)
        match_repo.create(m2)

        new_only = list(match_repo.list_by_user(user_id, status=MatchStatus.NEW.value))
        assert [m.id for m in new_only] == [m1.id]

    def test_returns_empty_when_callable_not_configured(
        self, match_repo: InMemoryVacancyMatchRepository
    ) -> None:
        # Strip the callable so the repo has no way to resolve the user.
        match_repo._list_user_profiles = None  # noqa: SLF001
        assert list(match_repo.list_by_user(uuid.uuid4())) == []


class TestFindExisting:
    def test_returns_existing_match(self, match_repo: InMemoryVacancyMatchRepository) -> None:
        profile_id = uuid.uuid4()
        vacancy_id = uuid.uuid4()
        match_repo.create(VacancyMatch(search_profile_id=profile_id, vacancy_id=vacancy_id))

        found = match_repo.find_existing(profile_id, vacancy_id)

        assert found is not None
        assert found.search_profile_id == profile_id
        assert found.vacancy_id == vacancy_id

    def test_returns_none_for_unknown(self, match_repo: InMemoryVacancyMatchRepository) -> None:
        assert match_repo.find_existing(uuid.uuid4(), uuid.uuid4()) is None


class TestUpdateStatus:
    def test_changes_status_and_score(self, match_repo: InMemoryVacancyMatchRepository) -> None:
        match = VacancyMatch(search_profile_id=uuid.uuid4(), vacancy_id=uuid.uuid4())
        match_repo.create(match)

        updated = match_repo.update_status(match.id, MatchStatus.SCORED.value, score=85)

        assert updated.status == MatchStatus.SCORED.value
        assert updated.score == 85
        assert match_repo.get_by_id(match.id).status == MatchStatus.SCORED.value

    def test_raises_for_unknown_match(self, match_repo: InMemoryVacancyMatchRepository) -> None:
        with pytest.raises(NotFoundError):
            match_repo.update_status(uuid.uuid4(), MatchStatus.ACCEPTED.value)


# ---------------------------------------------------------------------------
# SQL repository
# ---------------------------------------------------------------------------


@pytest.fixture
def sql_session_factory() -> Iterator:
    """Yield a ``sessionmaker`` bound to a fresh in-memory sqlite engine.

    The ``users`` table is needed for the search_profiles.user_id FK
    to resolve, so the in-memory engine is bootstrapped with the full
    metadata (which includes every imported model).
    """
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    try:
        yield Session
    finally:
        engine.dispose()


@pytest.fixture
def sql_repo(sql_session_factory) -> SqlVacancyMatchRepository:
    return SqlVacancyMatchRepository(session_factory=sql_session_factory)


def _seed_vacancy_and_profile(
    session_factory, *, user_id: uuid.UUID | None = None
) -> tuple[uuid.UUID, uuid.UUID]:
    """Persist a Vacancy and a SearchProfile and return their ids.

    We write through the raw session (not the repos) so the SQL
    repository tests can be unit-level rather than depending on the
    sources/search_profiles repos.
    """
    session = session_factory()
    try:
        vacancy = Vacancy(
            id=uuid.uuid4(),
            source="hh",
            source_id="hh-1",
            title="Python Dev",
            raw_data={},
        )
        session.add(vacancy)
        session.flush()
        owner_id = user_id or uuid.uuid4()
        # The users FK requires a users row; insert a minimal one.
        from apply_pilot.features.users.models import User

        user = User(id=owner_id, email=f"u{owner_id.hex[:8]}@example.com")
        session.add(user)
        session.flush()
        profile = SearchProfile(
            id=uuid.uuid4(),
            user_id=owner_id,
            title="Python",
            is_active=True,
        )
        session.add(profile)
        session.commit()
        return vacancy.id, profile.id
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


class TestSqlRepository:
    def test_create_persists_row(self, sql_repo: SqlVacancyMatchRepository) -> None:
        profile_id = uuid.uuid4()
        vacancy_id = uuid.uuid4()

        # We need a real users row for the FK chain in the SQL test.
        session_factory = sql_repo._session_factory  # noqa: SLF001
        from apply_pilot.features.users.models import User

        session = session_factory()
        try:
            user = User(id=uuid.uuid4(), email="x@example.com", hashed_password="pwhash")
            session.add(user)
            sp = SearchProfile(id=profile_id, user_id=user.id, title="t", is_active=True)
            v = Vacancy(id=vacancy_id, source="hh", source_id="s", title="t", raw_data={})
            session.add(sp)
            session.add(v)
            session.commit()
        finally:
            session.close()

        created = sql_repo.create(VacancyMatch(search_profile_id=profile_id, vacancy_id=vacancy_id))

        assert created.id is not None
        assert created.status == MatchStatus.NEW.value
        assert sql_repo.get_by_id(created.id) is not None

    def test_unique_constraint_blocks_duplicate_pair(
        self, sql_repo: SqlVacancyMatchRepository
    ) -> None:
        """Even bypassing the service, the DB unique constraint fires."""
        from sqlalchemy.exc import IntegrityError

        session_factory = sql_repo._session_factory  # noqa: SLF001
        from apply_pilot.features.users.models import User

        profile_id = uuid.uuid4()
        vacancy_id = uuid.uuid4()
        session = session_factory()
        try:
            user = User(id=uuid.uuid4(), email="dup@example.com", hashed_password="pwhash")
            session.add(user)
            sp = SearchProfile(id=profile_id, user_id=user.id, title="t", is_active=True)
            v = Vacancy(id=vacancy_id, source="hh", source_id="dup", title="t", raw_data={})
            session.add(sp)
            session.add(v)
            session.commit()
        finally:
            session.close()

        sql_repo.create(VacancyMatch(search_profile_id=profile_id, vacancy_id=vacancy_id))

        with pytest.raises(IntegrityError):
            session = session_factory()
            try:
                session.add(
                    VacancyMatch(
                        id=uuid.uuid4(),
                        search_profile_id=profile_id,
                        vacancy_id=vacancy_id,
                    )
                )
                session.commit()
            finally:
                session.close()

    def test_get_by_id_returns_none_for_unknown(self, sql_repo: SqlVacancyMatchRepository) -> None:
        assert sql_repo.get_by_id(uuid.uuid4()) is None

    def test_list_by_profile_returns_only_matching(
        self, sql_repo: SqlVacancyMatchRepository
    ) -> None:
        from apply_pilot.features.users.models import User

        p1, p2 = uuid.uuid4(), uuid.uuid4()
        session_factory = sql_repo._session_factory  # noqa: SLF001
        session = session_factory()
        try:
            user = User(id=uuid.uuid4(), email="multi@example.com", hashed_password="pwhash")
            session.add(user)
            session.add(SearchProfile(id=p1, user_id=user.id, title="A", is_active=True))
            session.add(SearchProfile(id=p2, user_id=user.id, title="B", is_active=True))
            v1 = Vacancy(id=uuid.uuid4(), source="hh", source_id="1", title="x", raw_data={})
            v2 = Vacancy(id=uuid.uuid4(), source="hh", source_id="2", title="y", raw_data={})
            v3 = Vacancy(id=uuid.uuid4(), source="hh", source_id="3", title="z", raw_data={})
            session.add_all([v1, v2, v3])
            session.flush()
            session.add_all(
                [
                    VacancyMatch(search_profile_id=p1, vacancy_id=v1.id),
                    VacancyMatch(search_profile_id=p1, vacancy_id=v2.id),
                    VacancyMatch(search_profile_id=p2, vacancy_id=v3.id),
                ]
            )
            session.commit()
        finally:
            session.close()

        assert len(list(sql_repo.list_by_profile(p1))) == 2
        assert len(list(sql_repo.list_by_profile(p2))) == 1

    def test_list_by_user_joins_through_search_profiles(
        self, sql_repo: SqlVacancyMatchRepository
    ) -> None:
        from apply_pilot.features.users.models import User

        owner_a = uuid.uuid4()
        owner_b = uuid.uuid4()
        session_factory = sql_repo._session_factory  # noqa: SLF001
        session = session_factory()
        try:
            session.add_all(
                [
                    User(id=owner_a, email="a@example.com", hashed_password="pwhash"),
                    User(id=owner_b, email="b@example.com", hashed_password="pwhash"),
                ]
            )
            pa = SearchProfile(id=uuid.uuid4(), user_id=owner_a, title="A", is_active=True)
            pb = SearchProfile(id=uuid.uuid4(), user_id=owner_b, title="B", is_active=True)
            session.add_all([pa, pb])
            session.flush()
            v = Vacancy(
                id=uuid.uuid4(),
                source="hh",
                source_id="join",
                title="t",
                raw_data={},
            )
            session.add(v)
            session.flush()
            session.add_all(
                [
                    VacancyMatch(search_profile_id=pa.id, vacancy_id=v.id),
                    VacancyMatch(search_profile_id=pb.id, vacancy_id=v.id),
                ]
            )
            session.commit()
        finally:
            session.close()

        assert len(list(sql_repo.list_by_user(owner_a))) == 1
        assert len(list(sql_repo.list_by_user(owner_b))) == 1

    def test_find_existing_returns_match(self, sql_repo: SqlVacancyMatchRepository) -> None:
        from apply_pilot.features.users.models import User

        session_factory = sql_repo._session_factory  # noqa: SLF001
        session = session_factory()
        try:
            user_id = uuid.uuid4()
            profile_id = uuid.uuid4()
            vacancy_id = uuid.uuid4()
            session.add(User(id=user_id, email="fe@example.com", hashed_password="pwhash"))
            session.add(SearchProfile(id=profile_id, user_id=user_id, title="t", is_active=True))
            session.add(Vacancy(id=vacancy_id, source="hh", source_id="fe", title="t", raw_data={}))
            session.flush()
            session.add(VacancyMatch(search_profile_id=profile_id, vacancy_id=vacancy_id))
            session.commit()
        finally:
            session.close()

        found = sql_repo.find_existing(profile_id, vacancy_id)
        assert found is not None
        assert found.search_profile_id == profile_id
        assert found.vacancy_id == vacancy_id

    def test_find_existing_returns_none_for_unknown(
        self, sql_repo: SqlVacancyMatchRepository
    ) -> None:
        assert sql_repo.find_existing(uuid.uuid4(), uuid.uuid4()) is None

    def test_update_status_persists(self, sql_repo: SqlVacancyMatchRepository) -> None:
        from apply_pilot.features.users.models import User

        session_factory = sql_repo._session_factory  # noqa: SLF001
        session = session_factory()
        try:
            user = User(id=uuid.uuid4(), email="us@example.com", hashed_password="pwhash")
            session.add(user)
            sp = SearchProfile(id=uuid.uuid4(), user_id=user.id, title="t", is_active=True)
            v = Vacancy(id=uuid.uuid4(), source="hh", source_id="us", title="t", raw_data={})
            session.add_all([sp, v])
            session.flush()
            m = VacancyMatch(search_profile_id=sp.id, vacancy_id=v.id)
            session.add(m)
            session.commit()
            match_id = m.id
        finally:
            session.close()

        updated = sql_repo.update_status(match_id, MatchStatus.SCORED.value, score=80)

        assert updated.status == MatchStatus.SCORED.value
        assert updated.score == 80

    def test_update_status_raises_not_found(self, sql_repo: SqlVacancyMatchRepository) -> None:
        with pytest.raises(NotFoundError):
            sql_repo.update_status(uuid.uuid4(), MatchStatus.ACCEPTED.value)

    def test_bulk_create_ignore_conflicts_inserts_and_skips(
        self, sql_repo: SqlVacancyMatchRepository
    ) -> None:
        from apply_pilot.features.users.models import User

        session_factory = sql_repo._session_factory  # noqa: SLF001
        session = session_factory()
        try:
            user = User(id=uuid.uuid4(), email="bulk@example.com", hashed_password="pwhash")
            session.add(user)
            sp = SearchProfile(id=uuid.uuid4(), user_id=user.id, title="t", is_active=True)
            v1 = Vacancy(id=uuid.uuid4(), source="hh", source_id="b1", title="t", raw_data={})
            v2 = Vacancy(id=uuid.uuid4(), source="hh", source_id="b2", title="t", raw_data={})
            session.add_all([sp, v1, v2])
            session.flush()
            # Pre-seed a match for (sp, v1) so the bulk insert must skip it.
            session.add(VacancyMatch(search_profile_id=sp.id, vacancy_id=v1.id))
            session.commit()
            profile_id, v1_id, v2_id = sp.id, v1.id, v2.id
        finally:
            session.close()

        new, dup = (
            VacancyMatch(search_profile_id=profile_id, vacancy_id=v1_id),
            VacancyMatch(search_profile_id=profile_id, vacancy_id=v2_id),
        )

        sql_repo.bulk_create_ignore_conflicts([new, dup])

        # v1 match already exists, v2 match was inserted.
        assert sql_repo.find_existing(profile_id, v1_id) is not None
        assert sql_repo.find_existing(profile_id, v2_id) is not None

    def test_repository_without_factory_raises(self) -> None:
        repo = SqlVacancyMatchRepository()
        with pytest.raises(RuntimeError, match="not bound"):
            repo.get_by_id(uuid.uuid4())
