"""Tests for the VacancyNormalizer.

Covers hh.ru API response mapping, salary normalization (gross→net,
hourly→monthly conversion that the hh API already provides as monthly),
missing fields, and content_hash computation.
"""

from __future__ import annotations

import pytest

from job_apply.features.sources.normalizer import VacancyNormalizer

# ---------------------------------------------------------------------------
# Realistic hh.ru API response fixture
# ---------------------------------------------------------------------------


@pytest.fixture
def hh_raw() -> dict:
    """A realistic hh.ru /vacancies/{id} API response (trimmed)."""
    return {
        "id": "114314622",
        "premium": False,
        "name": "Python-разработчик (Middle/Senior)",
        "area": {"id": "1", "name": "Москва", "url": "https://api.hh.ru/areas/1"},
        "salary": {
            "from": 250000,
            "to": 350000,
            "currency": "RUR",
            "gross": True,
        },
        "type": {"id": "open", "name": "Открытая"},
        "description": "Разработка и поддержка backend-сервисов на Python...",
        "url": "https://hh.ru/vacancy/114314622",
        "alternate_url": "https://hh.ru/vacancy/114314622",
        "employer": {
            "id": "12345",
            "name": "ООО Рога и Копыта",
            "url": "https://api.hh.ru/employers/12345",
            "alternate_url": "https://hh.ru/employer/12345",
            "trusted": True,
        },
        "schedule": {"id": "fullDay", "name": "Полный день"},
        "experience": {"id": "between1And3", "name": "От 1 года до 3 лет"},
        "employment": {"id": "full", "name": "Полная занятость"},
        "key_skills": [
            {"name": "Python"},
            {"name": "Django"},
            {"name": "PostgreSQL"},
            {"name": "Docker"},
        ],
        "published_at": "2025-12-01T10:00:00+0300",
        "created_at": "2025-11-30T15:00:00+0300",
        "initial_created_at": "2025-11-30T15:00:00+0300",
    }


@pytest.fixture
def hh_minimal() -> dict:
    """Minimal hh.ru response — no salary, no skills, no employer name."""
    return {
        "id": "999",
        "name": "Стажёр",
        "area": {"id": "2", "name": "Санкт-Петербург"},
        "salary": None,
        "employer": {"id": "1", "name": None},
        "schedule": None,
        "experience": None,
        "key_skills": [],
        "published_at": "2025-01-01T00:00:00+0300",
        "description": None,
        "url": None,
    }


@pytest.fixture
def hh_hourly_salary() -> dict:
    """hh.ru response with hourly salary (type=hourly, gross).

    Normalizer should convert hourly→monthly: hourly_rate * 168.
    """
    return {
        "id": "888",
        "name": "Курьер",
        "area": {"id": "2", "name": "Санкт-Петербург"},
        "salary": {
            "from": 300,
            "to": 400,
            "currency": "RUR",
            "gross": True,
            "type": {"id": "hourly", "name": "часовая"},
        },
        "employer": {"id": "999", "name": "Доставка ООО"},
        "schedule": None,
        "experience": None,
        "key_skills": [],
        "published_at": "2025-06-01T10:00:00+0300",
        "description": "Доставка заказов",
    }


# ---------------------------------------------------------------------------
# hh.ru normalization
# ---------------------------------------------------------------------------


class TestNormalizeHH:
    """Tests for normalize_hh method."""

    def test_maps_core_fields(self, hh_raw: dict) -> None:
        normalizer = VacancyNormalizer()
        vacancy = normalizer.normalize("hh", hh_raw)

        assert vacancy.source == "hh"
        assert vacancy.source_id == "114314622"
        assert vacancy.title == "Python-разработчик (Middle/Senior)"
        assert vacancy.location == "Москва"
        assert vacancy.employer_name == "ООО Рога и Копыта"
        assert vacancy.schedule == "Полный день"
        assert vacancy.experience == "От 1 года до 3 лет"
        assert vacancy.url == "https://hh.ru/vacancy/114314622"

    def test_maps_salary_fields(self, hh_raw: dict) -> None:
        normalizer = VacancyNormalizer()
        vacancy = normalizer.normalize("hh", hh_raw)

        # Gross→net normalization applied: 250000→217500, 350000→304500
        assert vacancy.salary_from == 217500
        assert vacancy.salary_to == 304500
        assert vacancy.salary_currency == "RUR"
        # Stored as net after normalization
        assert vacancy.salary_gross is False

    def test_maps_skills_as_list(self, hh_raw: dict) -> None:
        normalizer = VacancyNormalizer()
        vacancy = normalizer.normalize("hh", hh_raw)

        assert vacancy.skills == ["Python", "Django", "PostgreSQL", "Docker"]

    def test_maps_published_at(self, hh_raw: dict) -> None:
        normalizer = VacancyNormalizer()
        vacancy = normalizer.normalize("hh", hh_raw)

        assert vacancy.published_at is not None
        assert vacancy.published_at.year == 2025
        assert vacancy.published_at.month == 12
        assert vacancy.published_at.day == 1

    def test_stores_raw_data(self, hh_raw: dict) -> None:
        normalizer = VacancyNormalizer()
        vacancy = normalizer.normalize("hh", hh_raw)

        stored = vacancy.raw_data
        assert isinstance(stored, dict)
        assert stored["id"] == "114314622"
        assert stored["name"] == "Python-разработчик (Middle/Senior)"

    def test_computes_content_hash(self, hh_raw: dict) -> None:
        normalizer = VacancyNormalizer()
        vacancy = normalizer.normalize("hh", hh_raw)

        assert vacancy.content_hash is not None
        assert len(vacancy.content_hash) == 64  # SHA-256 hex digest

    def test_content_hash_changes_with_title(self, hh_raw: dict) -> None:
        normalizer = VacancyNormalizer()
        v1 = normalizer.normalize("hh", hh_raw)

        modified = {**hh_raw, "name": "Different title"}
        v2 = normalizer.normalize("hh", modified)

        assert v1.content_hash != v2.content_hash

    def test_minimal_response_no_salary(self, hh_minimal: dict) -> None:
        normalizer = VacancyNormalizer()
        vacancy = normalizer.normalize("hh", hh_minimal)

        assert vacancy.salary_from is None
        assert vacancy.salary_to is None
        assert vacancy.salary_currency == "RUR"
        assert vacancy.salary_gross is False
        assert vacancy.skills is None  # empty key_skills → None
        assert vacancy.schedule is None
        assert vacancy.experience is None
        assert vacancy.employer_name is None
        assert vacancy.description is None
        assert vacancy.url is None

    def test_salary_gross_defaults_false_when_absent(self) -> None:
        raw = {
            "id": "1",
            "name": "Test",
            "area": {"name": "Test"},
            "salary": {"from": 100, "to": 200, "currency": "RUR"},
            "employer": {"id": "1", "name": "Test"},
            "schedule": None,
            "experience": None,
            "key_skills": [],
            "published_at": "2025-01-01T00:00:00+0000",
        }
        normalizer = VacancyNormalizer()
        vacancy = normalizer.normalize("hh", raw)

        assert vacancy.salary_gross is False


# ---------------------------------------------------------------------------
# Salary normalization — gross → net
# ---------------------------------------------------------------------------


class TestSalaryNormalization:
    """Gross salary must be converted to net (×0.87 for Russia).

    When gross=True in the API response, the normalizer applies the 0.87
    coefficient and sets salary_gross=False on the stored model.
    """

    def test_gross_to_net_conversion(self, hh_raw: dict) -> None:
        normalizer = VacancyNormalizer()
        vacancy = normalizer.normalize("hh", hh_raw)

        # Gross salaries: 250000 → 217500, 350000 → 304500
        assert vacancy.salary_from == 217500
        assert vacancy.salary_to == 304500
        assert vacancy.salary_gross is False

    def test_net_salary_unchanged(self) -> None:
        raw = {
            "id": "1",
            "name": "Dev",
            "area": {"name": "Moscow"},
            "salary": {
                "from": 100000,
                "to": 200000,
                "currency": "RUR",
                "gross": False,
            },
            "employer": {"id": "1", "name": "Acme"},
            "schedule": None,
            "experience": None,
            "key_skills": [],
            "published_at": "2025-01-01T00:00:00+0000",
        }
        normalizer = VacancyNormalizer()
        vacancy = normalizer.normalize("hh", raw)

        assert vacancy.salary_from == 100000
        assert vacancy.salary_to == 200000

    def test_hourly_to_monthly_conversion(self, hh_hourly_salary: dict) -> None:
        normalizer = VacancyNormalizer()
        vacancy = normalizer.normalize("hh", hh_hourly_salary)

        # Hourly 300→400, gross → net: 300*168*0.87 ≈ 43848, 400*168*0.87 ≈ 58464
        assert vacancy.salary_from == 43848
        assert vacancy.salary_to == 58464

    def test_salary_only_from(self) -> None:
        """When only salary.from is present, salary.to remains None."""
        raw = {
            "id": "1",
            "name": "Dev",
            "area": {"name": "Moscow"},
            "salary": {"from": 100000, "currency": "RUR"},
            "employer": {"id": "1", "name": "Acme"},
            "schedule": None,
            "experience": None,
            "key_skills": [],
            "published_at": "2025-01-01T00:00:00+0000",
        }
        normalizer = VacancyNormalizer()
        vacancy = normalizer.normalize("hh", raw)

        assert vacancy.salary_from == 100000
        assert vacancy.salary_to is None


# ---------------------------------------------------------------------------
# Unknown source
# ---------------------------------------------------------------------------


class TestUnknownSource:
    """For unknown sources, normalize raises NotImplementedError."""

    def test_unknown_source_raises(self) -> None:
        normalizer = VacancyNormalizer()
        with pytest.raises(NotImplementedError, match="habr"):
            normalizer.normalize("habr", {"id": "1"})


# ---------------------------------------------------------------------------
# Vacancy creation (direct constructor test)
# ---------------------------------------------------------------------------


class TestVacancyConstruction:
    """Verify the Vacancy constructor works with typical arguments."""

    def test_create_minimal(self) -> None:
        from job_apply.features.sources.models import Vacancy

        v = Vacancy(source="hh", source_id="123", title="Test")
        assert v.source == "hh"
        assert v.source_id == "123"
        assert v.title == "Test"
        # Defaults for ``salary_currency`` and ``salary_gross`` are SQL-side
        # (server_default / default). They are populated by the flush, not
        # by the Python constructor — the normalizer always sets them
        # explicitly, so this is safe in practice.
