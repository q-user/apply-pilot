"""Tests for SourceService — the ingest_vacancy workflow.

The service is tested with the in-memory repository (the production
fake). The point of these tests is to verify the normaliser + repository
collaboration, not the repository mechanics (those live in
``test_vacancy_repository.py``).

Since M2 (issue #26) the service's :meth:`SourceService.ingest_vacancy`
returns ``list[ScreeningQuestion]`` — the captured screening
questions, or ``[]`` when no extractor was supplied. The vacancy
itself is observed through the repository's read methods; the tests
below use :meth:`VacancyRepository.find_by_source` /
:meth:`VacancyRepository.get_by_id` to keep the assertions honest.
"""

from __future__ import annotations

import pytest

from apply_pilot.features.sources.repository import InMemoryVacancyRepository
from apply_pilot.features.sources.service import SourceService


@pytest.fixture
def repo() -> InMemoryVacancyRepository:
    return InMemoryVacancyRepository()


@pytest.fixture
def service(repo: InMemoryVacancyRepository) -> SourceService:
    return SourceService(repo)


@pytest.fixture
def hh_raw() -> dict:
    """A realistic hh.ru payload, trimmed."""
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


def _get_vacancy(service: SourceService, source: str, source_id: str):
    rows = service.repo.find_by_source(source, source_id)
    assert rows, f"vacancy {source}:{source_id} not found in repo"
    return rows[0]


class TestIngestVacancy:
    def test_ingest_creates_new_vacancy(self, service: SourceService, hh_raw: dict) -> None:
        result = service.ingest_vacancy("hh", hh_raw)

        # No extractor → empty list returned.
        assert result == []
        vacancy = _get_vacancy(service, "hh", "114314622")
        assert vacancy.id is not None
        assert vacancy.source == "hh"
        assert vacancy.source_id == "114314622"
        assert vacancy.title == "Python-разработчик (Middle/Senior)"
        assert vacancy.created_at is not None

    def test_ingest_is_idempotent_on_same_natural_key(
        self, service: SourceService, hh_raw: dict
    ) -> None:
        service.ingest_vacancy("hh", hh_raw)
        service.ingest_vacancy("hh", {**hh_raw, "name": "Updated Title"})

        # Same canonical row, mutated in place.
        v1 = _get_vacancy(service, "hh", "114314622")
        assert v1.title == "Updated Title"
        # Two calls, but only one row stored.
        assert len(service.repo.list_by_source("hh")) == 1

    def test_ingest_persists_raw_payload_verbatim(
        self, service: SourceService, hh_raw: dict
    ) -> None:
        service.ingest_vacancy("hh", hh_raw)
        vacancy = _get_vacancy(service, "hh", "114314622")

        assert isinstance(vacancy.raw_data, dict)
        assert vacancy.raw_data["id"] == "114314622"
        assert vacancy.raw_data["employer"]["name"] == "ООО Рога и Копыта"

    def test_ingest_applies_salary_normalisation(
        self, service: SourceService, hh_raw: dict
    ) -> None:
        service.ingest_vacancy("hh", hh_raw)
        vacancy = _get_vacancy(service, "hh", "114314622")

        # Gross 250 000 → net 217 500; 350 000 → 304 500.
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
        service.ingest_vacancy("hh", raw)
        vacancy = _get_vacancy(service, "hh", "999")

        assert vacancy.salary_from is None
        assert vacancy.salary_to is None
        assert vacancy.salary_currency == "RUR"

    def test_ingest_different_source_creates_new_row(
        self, service: SourceService, hh_raw: dict
    ) -> None:
        service.ingest_vacancy("hh", hh_raw)
        hh_vacancy = _get_vacancy(service, "hh", "114314622")

        with pytest.raises(NotImplementedError):
            service.ingest_vacancy("habr", hh_raw)
        # The hh row is unaffected.
        assert service.repo.get_by_id(hh_vacancy.id) is not None

    def test_ingest_unknown_source_raises(self, service: SourceService) -> None:
        with pytest.raises(NotImplementedError, match="habr"):
            service.ingest_vacancy("habr", {"id": "1"})

    def test_repository_is_observable(self, service: SourceService, hh_raw: dict) -> None:
        service.ingest_vacancy("hh", hh_raw)
        vacancy = _get_vacancy(service, "hh", "114314622")

        # The repository exposes its state for end-to-end assertions.
        assert service.repo.get_by_id(vacancy.id) is not None
        assert len(service.repo.list_by_source("hh")) == 1
