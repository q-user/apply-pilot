"""Tests for SourceService — the ingest_vacancy workflow.

Verifies that normalizing + upserting raw source data works end to end.
"""

from __future__ import annotations

import pytest

from job_apply.features.sources.repository import InMemoryVacancyRepository
from job_apply.features.sources.service import SourceService


@pytest.fixture
def repo() -> InMemoryVacancyRepository:
    return InMemoryVacancyRepository()


@pytest.fixture
def service(repo: InMemoryVacancyRepository) -> SourceService:
    return SourceService(repo)


@pytest.fixture
def hh_raw() -> dict:
    return {
        "id": "114314622",
        "name": "Python-разработчик (Middle/Senior)",
        "area": {"id": "1", "name": "Москва"},
        "salary": {
            "from": 250000,
            "to": 350000,
            "currency": "RUR",
            "gross": True,
        },
        "employer": {"id": "12345", "name": "ООО Рога и Копыта"},
        "schedule": {"id": "fullDay", "name": "Полный день"},
        "experience": {"id": "between1And3", "name": "От 1 года до 3 лет"},
        "key_skills": [
            {"name": "Python"},
            {"name": "Django"},
        ],
        "published_at": "2025-12-01T10:00:00+0300",
        "description": "Разработка backend-сервисов",
        "url": "https://hh.ru/vacancy/114314622",
    }


class TestIngestVacancy:
    def test_ingest_creates_new_vacancy(self, service: SourceService, hh_raw: dict) -> None:
        vacancy = service.ingest_vacancy("hh", hh_raw)

        assert vacancy.id is not None
        assert vacancy.source == "hh"
        assert vacancy.source_id == "114314622"
        assert vacancy.title == "Python-разработчик (Middle/Senior)"

    def test_ingest_idempotent(self, service: SourceService, hh_raw: dict) -> None:
        v1 = service.ingest_vacancy("hh", hh_raw)

        # Slightly different raw data — same source+source_id
        modified = {**hh_raw, "name": "Updated Title"}
        v2 = service.ingest_vacancy("hh", modified)

        assert v2.id == v1.id
        assert v2.title == "Updated Title"

    def test_ingest_stores_raw_data(self, service: SourceService, hh_raw: dict) -> None:
        vacancy = service.ingest_vacancy("hh", hh_raw)

        assert isinstance(vacancy.raw_data, dict)
        assert vacancy.raw_data["id"] == "114314622"

    def test_ingest_salary_normalization(self, service: SourceService, hh_raw: dict) -> None:
        vacancy = service.ingest_vacancy("hh", hh_raw)

        # Gross → net
        assert vacancy.salary_from == 217500
        assert vacancy.salary_to == 304500
        assert vacancy.salary_gross is False

    def test_ingest_without_salary(self, service: SourceService) -> None:
        raw = {
            "id": "999",
            "name": "Без зарплаты",
            "area": {"name": "СПб"},
            "salary": None,
            "employer": {"id": "1", "name": "Acme"},
            "schedule": None,
            "experience": None,
            "key_skills": [],
            "published_at": "2025-01-01T00:00:00+0000",
        }
        vacancy = service.ingest_vacancy("hh", raw)

        assert vacancy.salary_from is None
        assert vacancy.salary_to is None
