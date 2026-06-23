"""Tests for VacancyRepository implementations.

The in-memory fake is tested in detail; the SQL implementation is
exercised end-to-end with an in-memory sqlite engine so we can verify
the dialect-aware upsert behaves as advertised.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from apply_pilot.db import Base
from apply_pilot.features.audit import models as _audit_models  # noqa: F401
from apply_pilot.features.resumes import models as _resumes_models  # noqa: F401
from apply_pilot.features.search_profiles import models as _sp_models  # noqa: F401
from apply_pilot.features.sources.models import Vacancy
from apply_pilot.features.sources.repository import (
    InMemoryVacancyRepository,
    SqlVacancyRepository,
    VacancyRepository,
)

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _vacancy(source_id: str = "hh-123", title: str = "Python Dev", **overrides) -> Vacancy:
    """Build a ``Vacancy`` populated with sensible defaults.

    Mirrors what the real normaliser produces: ``salary_gross`` is always
    set to ``False`` (the model only stores net values) and the currency
    defaults to ``RUR``.
    """
    payload: dict = {
        "source": "hh",
        "source_id": source_id,
        "title": title,
        "location": "Moscow",
        "salary_from": 100000,
        "salary_to": 200000,
        "salary_currency": "RUR",
        "salary_gross": False,
        "raw_data": {"id": source_id, "name": title},
    }
    payload.update(overrides)
    return Vacancy(**payload)


# ---------------------------------------------------------------------------
# In-memory repository
# ---------------------------------------------------------------------------


@pytest.fixture
def repo() -> InMemoryVacancyRepository:
    return InMemoryVacancyRepository()


class TestUpsert:
    def test_upsert_inserts_new(self, repo: VacancyRepository) -> None:
        result = repo.upsert(_vacancy())

        assert result.id is not None
        assert result.source == "hh"
        assert result.source_id == "hh-123"
        assert result.title == "Python Dev"
        assert result.created_at is not None
        assert result.updated_at is not None

    def test_upsert_returns_same_record_on_second_call(self, repo: VacancyRepository) -> None:
        first = repo.upsert(_vacancy())
        original_id = first.id
        original_created_at = first.created_at

        second = repo.upsert(_vacancy(title="Updated Title"))

        # Same natural key → same row, same id.
        assert second.id == original_id
        assert second.title == "Updated Title"
        # created_at must NOT be reset on update.
        assert second.created_at == original_created_at
        # updated_at must advance.
        assert second.updated_at >= original_created_at

    def test_upsert_replaces_mutable_fields(self, repo: VacancyRepository) -> None:
        repo.upsert(_vacancy(title="Original", salary_from=100, salary_to=200))
        second = repo.upsert(_vacancy(title="New", salary_from=300, salary_to=400))

        assert second.title == "New"
        assert second.salary_from == 300
        assert second.salary_to == 400

    def test_upsert_different_source_id_creates_new(self, repo: VacancyRepository) -> None:
        r1 = repo.upsert(_vacancy(source_id="hh-1"))
        r2 = repo.upsert(_vacancy(source_id="hh-2"))

        assert r2.id != r1.id
        assert r2.source_id == "hh-2"

    def test_upsert_different_source_creates_new(self, repo: VacancyRepository) -> None:
        r1 = repo.upsert(_vacancy(source_id="123"))

        # Build a fresh vacancy from a different source. The natural key
        # is (source, source_id), so (habr, 123) must produce a new row.
        habr = _vacancy(source_id="123", title="Habr Vacancy")
        habr.source = "habr"
        r2 = repo.upsert(habr)

        assert r2.id != r1.id
        assert r2.source == "habr"


class TestGetById:
    def test_get_by_id_returns_vacancy(self, repo: VacancyRepository) -> None:
        created = repo.upsert(_vacancy())
        fetched = repo.get_by_id(created.id)

        assert fetched is not None
        assert fetched.id == created.id
        assert fetched.title == created.title

    def test_get_by_id_returns_none_for_unknown(self, repo: VacancyRepository) -> None:
        assert repo.get_by_id(uuid.uuid4()) is None


class TestListBySource:
    def test_list_by_source_returns_matching(self, repo: VacancyRepository) -> None:
        repo.upsert(_vacancy(source_id="1", title="A"))
        repo.upsert(_vacancy(source_id="2", title="B"))
        habr_v = _vacancy(source_id="1", title="C")
        habr_v.source = "habr"
        repo.upsert(habr_v)

        assert len(repo.list_by_source("hh")) == 2
        assert len(repo.list_by_source("habr")) == 1

    def test_list_by_source_empty(self, repo: VacancyRepository) -> None:
        assert repo.list_by_source("hh") == []


class TestListRecent:
    def test_list_recent_respects_limit(self, repo: VacancyRepository) -> None:
        for i in range(5):
            repo.upsert(_vacancy(source_id=str(i)))

        assert len(repo.list_recent(limit=3)) == 3

    def test_list_recent_returns_most_recent_first(self, repo: VacancyRepository) -> None:
        # Insert with explicit increasing created_at by faking the clock:
        # the in-memory repo sorts by created_at desc, so the last insert
        # appears first.
        a = repo.upsert(_vacancy(source_id="a", title="A"))
        b = repo.upsert(_vacancy(source_id="b", title="B"))

        recent = list(repo.list_recent(limit=10))
        assert recent[0].id == b.id
        assert recent[1].id == a.id


class TestGetByIds:
    def test_get_by_ids_returns_matching_vacancies(self, repo: VacancyRepository) -> None:
        v1 = repo.upsert(_vacancy(source_id="1", title="Vacancy 1"))
        v2 = repo.upsert(_vacancy(source_id="2", title="Vacancy 2"))
        v3 = repo.upsert(_vacancy(source_id="3", title="Vacancy 3"))

        result = list(repo.get_by_ids([v1.id, v3.id]))

        assert len(result) == 2
        assert {v.id for v in result} == {v1.id, v3.id}

    def test_get_by_ids_returns_empty_for_unknown_ids(self, repo: VacancyRepository) -> None:
        repo.upsert(_vacancy(source_id="1"))
        result = list(repo.get_by_ids([uuid.uuid4(), uuid.uuid4()]))
        assert result == []

    def test_get_by_ids_returns_empty_for_empty_list(self, repo: VacancyRepository) -> None:
        result = list(repo.get_by_ids([]))
        assert result == []

    def test_get_by_ids_handles_duplicates(self, repo: VacancyRepository) -> None:
        v1 = repo.upsert(_vacancy(source_id="1"))
        result = list(repo.get_by_ids([v1.id, v1.id]))
        assert len(result) == 1
        assert result[0].id == v1.id


# ---------------------------------------------------------------------------
# SQL repository
# ---------------------------------------------------------------------------


@pytest.fixture
def sql_session_factory() -> Iterator:
    """Yield a ``sessionmaker`` bound to a fresh in-memory sqlite engine."""
    engine = create_engine("sqlite:///:memory:")
    # Importing the modules here ensures the metadata is fully populated
    # before ``create_all`` runs (avoids the FK resolution order problem).
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    try:
        yield Session
    finally:
        engine.dispose()


@pytest.fixture
def sql_repo(sql_session_factory) -> SqlVacancyRepository:
    return SqlVacancyRepository(session_factory=sql_session_factory)


class TestSqlRepository:
    def test_upsert_inserts_and_returns_row(self, sql_repo: SqlVacancyRepository) -> None:
        v = sql_repo.upsert(_vacancy())
        assert v.id is not None
        assert v.created_at is not None
        assert v.salary_from == 100000

    def test_upsert_updates_existing_row_in_place(self, sql_repo: SqlVacancyRepository) -> None:
        first = sql_repo.upsert(_vacancy())
        second = sql_repo.upsert(_vacancy(title="Updated", salary_from=999))

        assert second.id == first.id
        assert second.title == "Updated"
        assert second.salary_from == 999
        # Re-fetching returns the updated state.
        fetched = sql_repo.get_by_id(first.id)
        assert fetched is not None
        assert fetched.title == "Updated"
        assert fetched.salary_from == 999

    def test_unique_constraint_rejects_duplicate_natural_key(
        self, sql_repo: SqlVacancyRepository
    ) -> None:
        """A direct second insert with the same natural key must raise.

        This is the safety net behind the upsert: even without the
        ``ON CONFLICT`` clause, the unique constraint is the source of
        truth.
        """
        from sqlalchemy.exc import IntegrityError

        v = sql_repo.upsert(_vacancy())
        # Build a brand-new ORM instance with the same (source, source_id).
        with pytest.raises(IntegrityError):
            new_session_factory = sql_repo._session_factory  # noqa: SLF001
            session = new_session_factory()
            try:
                dup = Vacancy(
                    id=uuid.uuid4(),
                    source=v.source,
                    source_id=v.source_id,
                    title="dup",
                    raw_data={},
                )
                session.add(dup)
                session.commit()
            finally:
                session.close()

    def test_list_by_source_filters(self, sql_repo: SqlVacancyRepository) -> None:
        sql_repo.upsert(_vacancy(source_id="1"))
        sql_repo.upsert(_vacancy(source_id="2"))
        habr_v = _vacancy(source_id="1", title="H")
        habr_v.source = "habr"
        sql_repo.upsert(habr_v)

        assert len(sql_repo.list_by_source("hh")) == 2
        assert len(sql_repo.list_by_source("habr")) == 1

    def test_list_recent_respects_limit(self, sql_repo: SqlVacancyRepository) -> None:
        for i in range(5):
            sql_repo.upsert(_vacancy(source_id=str(i)))
        assert len(sql_repo.list_recent(limit=3)) == 3

    def test_repository_without_factory_raises(self) -> None:
        repo = SqlVacancyRepository()
        with pytest.raises(RuntimeError, match="not bound"):
            repo.get_by_id(uuid.uuid4())

    def test_upsert_applies_column_defaults_when_caller_leaves_them_unset(
        self, sql_repo: SqlVacancyRepository
    ) -> None:
        """A Vacancy built without salary_gross/currency still inserts cleanly.

        Core-level ``INSERT`` statements do not run the ORM column
        defaults; the SQL repository must apply them itself.
        """
        v = Vacancy(
            source="hh",
            source_id="sparse",
            title="Sparse",
            salary_gross=None,  # type: ignore[arg-type]
            salary_currency=None,  # type: ignore[arg-type]
            raw_data={},
        )
        persisted = sql_repo.upsert(v)
        assert persisted.salary_gross is False
        assert persisted.salary_currency == "RUR"

    def test_get_by_ids_returns_matching_vacancies(self, sql_repo: SqlVacancyRepository) -> None:
        v1 = sql_repo.upsert(_vacancy(source_id="1", title="Vacancy 1"))
        v2 = sql_repo.upsert(_vacancy(source_id="2", title="Vacancy 2"))
        v3 = sql_repo.upsert(_vacancy(source_id="3", title="Vacancy 3"))

        result = list(sql_repo.get_by_ids([v1.id, v3.id]))

        assert len(result) == 2
        assert {v.id for v in result} == {v1.id, v3.id}

    def test_get_by_ids_returns_empty_for_unknown_ids(self, sql_repo: SqlVacancyRepository) -> None:
        sql_repo.upsert(_vacancy(source_id="1"))
        result = list(sql_repo.get_by_ids([uuid.uuid4(), uuid.uuid4()]))
        assert result == []

    def test_get_by_ids_returns_empty_for_empty_list(self, sql_repo: SqlVacancyRepository) -> None:
        result = list(sql_repo.get_by_ids([]))
        assert result == []

    def test_get_by_ids_handles_duplicates(self, sql_repo: SqlVacancyRepository) -> None:
        v1 = sql_repo.upsert(_vacancy(source_id="1"))
        result = list(sql_repo.get_by_ids([v1.id, v1.id]))
        assert len(result) == 1
        assert result[0].id == v1.id


# ---------------------------------------------------------------------------
# Filter-aware list/count (powers GET /vacancies)
# ---------------------------------------------------------------------------


def _store_with_created_at(
    repo: VacancyRepository,
    *,
    source: str,
    source_id: str,
    created_at,
    **overrides,
) -> Vacancy:
    """Persist a Vacancy and pin its ``created_at`` for deterministic ordering.

    Both implementations stamp ``created_at = now`` on insert, so we
    need to mutate the row in place to set a known timestamp.
    """
    payload = {
        "source": source,
        "title": overrides.pop("title", f"V-{source_id}"),
        "location": overrides.pop("location", "Moscow"),
        "salary_from": overrides.pop("salary_from", 100000),
        "salary_to": overrides.pop("salary_to", 200000),
        "salary_currency": "RUR",
        "salary_gross": False,
        "raw_data": {"id": source_id},
    }
    payload.update(overrides)
    row = repo.upsert(Vacancy(source_id=source_id, **payload))
    row.created_at = created_at
    return row


class TestInMemoryFilterList:
    def test_list_with_filters_returns_newest_first(self, repo: InMemoryVacancyRepository) -> None:
        old = _store_with_created_at(
            repo, source="hh", source_id="a", created_at=datetime(2024, 1, 1, tzinfo=UTC)
        )
        new = _store_with_created_at(
            repo, source="hh", source_id="b", created_at=datetime(2024, 6, 1, tzinfo=UTC)
        )
        items = list(
            repo.list_with_filters(
                source="hh", salary_min=None, location=None, since=None, limit=10, offset=0
            )
        )
        assert [v.id for v in items] == [new.id, old.id]

    def test_list_with_filters_combines_all_filters(self, repo: InMemoryVacancyRepository) -> None:
        match = _store_with_created_at(
            repo,
            source="hh",
            source_id="match",
            created_at=datetime(2024, 6, 1, tzinfo=UTC),
            title="Match",
            salary_from=200_000,
            location="Moscow",
        )
        _store_with_created_at(
            repo,
            source="habr",
            source_id="other",
            created_at=datetime(2024, 6, 1, tzinfo=UTC),
            title="Other",
            salary_from=200_000,
            location="Moscow",
        )
        items = list(
            repo.list_with_filters(
                source="hh",
                salary_min=100_000,
                location="moscow",
                since=datetime(2024, 1, 1, tzinfo=UTC),
                limit=10,
                offset=0,
            )
        )
        assert len(items) == 1
        assert items[0].id == match.id

    def test_count_with_filters_matches_list_length(self, repo: InMemoryVacancyRepository) -> None:
        for i in range(3):
            _store_with_created_at(
                repo,
                source="hh",
                source_id=f"hh-{i}",
                created_at=datetime(2024, 1, 1, tzinfo=UTC) + timedelta(days=i),
            )
        _store_with_created_at(
            repo,
            source="habr",
            source_id="habr-1",
            created_at=datetime(2024, 6, 1, tzinfo=UTC),
        )
        total = repo.count_with_filters(source="hh")
        items = list(
            repo.list_with_filters(
                source="hh", salary_min=None, location=None, since=None, limit=10, offset=0
            )
        )
        assert total == 3
        assert len(items) == 3

    def test_list_with_filters_respects_offset_and_limit(
        self, repo: InMemoryVacancyRepository
    ) -> None:
        for i in range(5):
            _store_with_created_at(
                repo,
                source="hh",
                source_id=f"hh-{i}",
                created_at=datetime(2024, 1, 1, tzinfo=UTC) + timedelta(minutes=i),
                title=f"V{i}",
            )
        # Newest first → V4, V3, V2, V1, V0; skip the first 2.
        items = list(
            repo.list_with_filters(
                source=None, salary_min=None, location=None, since=None, limit=2, offset=2
            )
        )
        assert [v.title for v in items] == ["V2", "V1"]


class TestSqlFilterList:
    def test_list_with_filters_applies_all_predicates(self, sql_repo: SqlVacancyRepository) -> None:
        """All four filters must compose as a logical AND.

        Insertion order does not need to be deterministic — the
        ``created_at`` column has a server-side default (``func.now()``)
        so a client-side override does not stick. Asserting only on
        the filtered subset keeps the test robust against clock
        resolution on the in-memory SQLite engine.
        """
        for sid, salary in [("a", 100_000), ("b", 200_000), ("c", 350_000)]:
            _store_with_created_at(
                sql_repo,
                source="hh",
                source_id=sid,
                created_at=datetime(2024, 1, 1, tzinfo=UTC),
                salary_from=salary,
                location="Moscow",
            )
        _store_with_created_at(
            sql_repo,
            source="habr",
            source_id="d",
            created_at=datetime(2024, 1, 1, tzinfo=UTC),
            salary_from=200_000,
            location="Moscow",
        )

        items = list(
            sql_repo.list_with_filters(
                source="hh",
                salary_min=150_000,
                location="moscow",
                since=datetime(2023, 1, 1, tzinfo=UTC),
                limit=10,
                offset=0,
            )
        )
        assert {v.source_id for v in items} == {"b", "c"}
        assert sql_repo.count_with_filters(source="hh", salary_min=150_000, location="moscow") == 2
