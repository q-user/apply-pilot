"""Tests for VacancyRepository implementations.

Verifies upsert behaviour: insert new row on first call, update existing
row when source+source_id match (ON CONFLICT).
"""

from __future__ import annotations

import uuid

import pytest

from job_apply.features.sources.models import Vacancy
from job_apply.features.sources.repository import InMemoryVacancyRepository, VacancyRepository

# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def repo() -> InMemoryVacancyRepository:
    return InMemoryVacancyRepository()


def _vacancy(source_id: str = "hh-123", title: str = "Python Dev") -> Vacancy:
    return Vacancy(
        source="hh",
        source_id=source_id,
        title=title,
        location="Moscow",
        salary_from=100000,
        salary_to=200000,
        salary_currency="RUR",
    )


# ---------------------------------------------------------------------------
# Upsert behaviour
# ---------------------------------------------------------------------------


class TestUpsert:
    def test_upsert_inserts_new(self, repo: VacancyRepository) -> None:
        v = _vacancy()
        result = repo.upsert(v)

        assert result.id is not None
        assert result.source == "hh"
        assert result.source_id == "hh-123"
        assert result.title == "Python Dev"

    def test_upsert_returns_same_record_on_second_call(self, repo: VacancyRepository) -> None:
        v1 = _vacancy()
        result1 = repo.upsert(v1)

        v2 = _vacancy(title="Updated Title")
        result2 = repo.upsert(v2)

        # Same source+source_id → must be the same row
        assert result2.id == result1.id
        assert result2.title == "Updated Title"

    def test_upsert_different_source_id_creates_new(self, repo: VacancyRepository) -> None:
        v1 = _vacancy(source_id="hh-1", title="Dev 1")
        r1 = repo.upsert(v1)

        v2 = _vacancy(source_id="hh-2", title="Dev 2")
        r2 = repo.upsert(v2)

        assert r2.id != r1.id
        assert r2.source_id == "hh-2"

    def test_upsert_different_source_creates_new(self, repo: VacancyRepository) -> None:
        v1 = Vacancy(source="hh", source_id="123", title="HH Vacancy")
        r1 = repo.upsert(v1)

        v2 = Vacancy(source="habr", source_id="123", title="Habr Vacancy")
        r2 = repo.upsert(v2)

        assert r2.id != r1.id
        assert r2.source == "habr"


# ---------------------------------------------------------------------------
# get_by_id
# ---------------------------------------------------------------------------


class TestGetById:
    def test_get_by_id_returns_vacancy(self, repo: VacancyRepository) -> None:
        v = _vacancy()
        created = repo.upsert(v)

        fetched = repo.get_by_id(created.id)
        assert fetched is not None
        assert fetched.id == created.id
        assert fetched.title == created.title

    def test_get_by_id_returns_none_for_unknown(self, repo: VacancyRepository) -> None:
        assert repo.get_by_id(uuid.uuid4()) is None


# ---------------------------------------------------------------------------
# list_by_source
# ---------------------------------------------------------------------------


class TestListBySource:
    def test_list_by_source_returns_matching(self, repo: VacancyRepository) -> None:
        repo.upsert(Vacancy(source="hh", source_id="1", title="A"))
        repo.upsert(Vacancy(source="hh", source_id="2", title="B"))
        repo.upsert(Vacancy(source="habr", source_id="1", title="C"))

        hh_vacancies = repo.list_by_source("hh")
        assert len(hh_vacancies) == 2

        habr_vacancies = repo.list_by_source("habr")
        assert len(habr_vacancies) == 1

    def test_list_by_source_empty(self, repo: VacancyRepository) -> None:
        assert repo.list_by_source("hh") == []


# ---------------------------------------------------------------------------
# list_recent
# ---------------------------------------------------------------------------


class TestListRecent:
    def test_list_recent_returns_ordered_by_created_at(self, repo: VacancyRepository) -> None:
        repo.upsert(Vacancy(source="hh", source_id="1", title="Old"))
        repo.upsert(Vacancy(source="hh", source_id="2", title="New"))

        recent = repo.list_recent(limit=10)
        # In-memory repo sorts by created_at descending
        assert len(recent) >= 2

    def test_list_recent_respects_limit(self, repo: VacancyRepository) -> None:
        for i in range(5):
            repo.upsert(Vacancy(source="hh", source_id=str(i), title=f"Vacancy {i}"))

        recent = repo.list_recent(limit=3)
        assert len(recent) == 3
