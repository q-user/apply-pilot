"""Tests for the VacancyNormalizer.

Covers hh.ru API response mapping (a realistic payload is used as the
fixture, lifted from the public ``/vacancies/{id}`` response schema),
salary normalisation (gross → net, hourly → monthly), tolerance to
missing fields, the ``content_hash`` derivation, and dispatch behaviour
for unknown sources.
"""

from __future__ import annotations

from datetime import datetime

import pytest

from apply_pilot.features.sources.normalizer import (
    VacancyNormalizer,
    _parse_hh_datetime,
    compute_content_hash,
)

# ---------------------------------------------------------------------------
# Realistic hh.ru /vacancies/{id} response fixture
# ---------------------------------------------------------------------------
#
# The fixture below is a trimmed copy of the public hh.ru response schema
# (see https://github.com/hhru/api). Cyrillic text is preserved verbatim
# to exercise UTF-8 handling in the content-hash path.


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
    """hh.ru response with an hourly, gross salary.

    Expected pipeline: 300 × 168 × 0.87 = 43 848, 400 × 168 × 0.87 = 58 464.
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
# hh.ru normalisation
# ---------------------------------------------------------------------------


class TestNormalizeHH:
    """End-to-end mapping of the hh.ru payload into the canonical Vacancy."""

    def test_maps_core_fields(self, hh_raw: dict) -> None:
        vacancy = VacancyNormalizer().normalize("hh", hh_raw)

        assert vacancy.source == "hh"
        assert vacancy.source_id == "114314622"
        assert vacancy.title == "Python-разработчик (Middle/Senior)"
        assert vacancy.location == "Москва"
        assert vacancy.employer_name == "ООО Рога и Копыта"
        assert vacancy.schedule == "Полный день"
        assert vacancy.experience == "От 1 года до 3 лет"
        assert vacancy.url == "https://hh.ru/vacancy/114314622"

    def test_maps_salary_fields(self, hh_raw: dict) -> None:
        vacancy = VacancyNormalizer().normalize("hh", hh_raw)

        # Gross 250 000 → net 217 500; 350 000 → 304 500.
        assert vacancy.salary_from == 217500
        assert vacancy.salary_to == 304500
        assert vacancy.salary_currency == "RUR"
        # Net-only storage: the gross flag is always False post-normalisation.
        assert vacancy.salary_gross is False

    def test_maps_skills_as_list(self, hh_raw: dict) -> None:
        vacancy = VacancyNormalizer().normalize("hh", hh_raw)

        assert vacancy.skills == ["Python", "Django", "PostgreSQL", "Docker"]

    def test_maps_published_at_to_utc(self, hh_raw: dict) -> None:
        vacancy = VacancyNormalizer().normalize("hh", hh_raw)

        assert vacancy.published_at is not None
        assert vacancy.published_at.tzinfo is not None
        assert vacancy.published_at.year == 2025
        assert vacancy.published_at.month == 12
        assert vacancy.published_at.day == 1
        # +0300 local → 07:00 UTC.
        assert vacancy.published_at.hour == 7
        assert vacancy.published_at.minute == 0

    def test_stores_raw_data_verbatim(self, hh_raw: dict) -> None:
        vacancy = VacancyNormalizer().normalize("hh", hh_raw)

        assert isinstance(vacancy.raw_data, dict)
        assert vacancy.raw_data["id"] == "114314622"
        assert vacancy.raw_data["name"] == "Python-разработчик (Middle/Senior)"
        # Defensive copy: mutating the original should not affect the stored copy.
        hh_raw["name"] = "mutated"
        assert vacancy.raw_data["name"] == "Python-разработчик (Middle/Senior)"

    def test_computes_content_hash(self, hh_raw: dict) -> None:
        vacancy = VacancyNormalizer().normalize("hh", hh_raw)

        assert vacancy.content_hash is not None
        assert len(vacancy.content_hash) == 64  # SHA-256 hex digest length
        # Stable across re-normalisation of the same payload.
        again = VacancyNormalizer().normalize("hh", hh_raw)
        assert vacancy.content_hash == again.content_hash

    def test_content_hash_changes_with_title(self, hh_raw: dict) -> None:
        v1 = VacancyNormalizer().normalize("hh", hh_raw)
        v2 = VacancyNormalizer().normalize("hh", {**hh_raw, "name": "Different title"})
        assert v1.content_hash != v2.content_hash

    def test_content_hash_changes_with_employer(self, hh_raw: dict) -> None:
        v1 = VacancyNormalizer().normalize("hh", hh_raw)
        v2 = VacancyNormalizer().normalize(
            "hh", {**hh_raw, "employer": {**hh_raw["employer"], "name": "Other Co"}}
        )
        assert v1.content_hash != v2.content_hash

    def test_minimal_response_no_salary(self, hh_minimal: dict) -> None:
        vacancy = VacancyNormalizer().normalize("hh", hh_minimal)

        assert vacancy.salary_from is None
        assert vacancy.salary_to is None
        assert vacancy.salary_currency == "RUR"
        assert vacancy.salary_gross is False
        assert vacancy.skills is None  # empty list → None
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
        vacancy = VacancyNormalizer().normalize("hh", raw)

        assert vacancy.salary_gross is False
        # And the values are passed through unchanged (no gross flag → no
        # 0.87 coefficient applied).
        assert vacancy.salary_from == 100
        assert vacancy.salary_to == 200

    def test_url_falls_back_to_alternate_url(self) -> None:
        raw = {
            "id": "1",
            "name": "Test",
            "area": {"name": "X"},
            "salary": None,
            "employer": {"id": "1", "name": "Y"},
            "schedule": None,
            "experience": None,
            "key_skills": [],
            "published_at": "2025-01-01T00:00:00+0000",
            "alternate_url": "https://hh.ru/alt/1",
        }
        vacancy = VacancyNormalizer().normalize("hh", raw)
        assert vacancy.url == "https://hh.ru/alt/1"

    def test_skills_silently_skip_blank_entries(self) -> None:
        raw = {
            "id": "1",
            "name": "T",
            "area": {"name": "X"},
            "salary": None,
            "employer": {"id": "1", "name": "Y"},
            "schedule": None,
            "experience": None,
            "key_skills": [{"name": "Python"}, {"name": ""}, {}, {"name": "Django"}],
            "published_at": "2025-01-01T00:00:00+0000",
        }
        vacancy = VacancyNormalizer().normalize("hh", raw)
        assert vacancy.skills == ["Python", "Django"]


# ---------------------------------------------------------------------------
# Salary normalisation
# ---------------------------------------------------------------------------


class TestSalaryNormalization:
    """The salary helpers are the only stateful part of the normaliser.

    They deserve their own group of tests so the edge cases are obvious.
    """

    def test_gross_to_net_conversion(self, hh_raw: dict) -> None:
        vacancy = VacancyNormalizer().normalize("hh", hh_raw)

        # 250 000 × 0.87 = 217 500; 350 000 × 0.87 = 304 500.
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
        vacancy = VacancyNormalizer().normalize("hh", raw)

        assert vacancy.salary_from == 100000
        assert vacancy.salary_to == 200000
        assert vacancy.salary_gross is False

    def test_hourly_to_monthly_conversion(self, hh_hourly_salary: dict) -> None:
        vacancy = VacancyNormalizer().normalize("hh", hh_hourly_salary)

        # Hourly 300 × 168 = 50 400 → gross-net 43 848.
        # Hourly 400 × 168 = 67 200 → gross-net 58 464.
        assert vacancy.salary_from == 43848
        assert vacancy.salary_to == 58464

    def test_salary_only_from_present(self) -> None:
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
        vacancy = VacancyNormalizer().normalize("hh", raw)

        assert vacancy.salary_from == 100000
        assert vacancy.salary_to is None

    def test_salary_only_to_present(self) -> None:
        raw = {
            "id": "1",
            "name": "Dev",
            "area": {"name": "Moscow"},
            "salary": {"to": 200000, "currency": "RUR"},
            "employer": {"id": "1", "name": "Acme"},
            "schedule": None,
            "experience": None,
            "key_skills": [],
            "published_at": "2025-01-01T00:00:00+0000",
        }
        vacancy = VacancyNormalizer().normalize("hh", raw)

        assert vacancy.salary_from is None
        assert vacancy.salary_to == 200000

    def test_salary_currency_defaults_to_rur(self) -> None:
        raw = {
            "id": "1",
            "name": "Dev",
            "area": {"name": "X"},
            "salary": {"from": 100, "to": 200},  # no currency
            "employer": {"id": "1", "name": "Y"},
            "schedule": None,
            "experience": None,
            "key_skills": [],
            "published_at": "2025-01-01T00:00:00+0000",
        }
        vacancy = VacancyNormalizer().normalize("hh", raw)
        assert vacancy.salary_currency == "RUR"

    def test_rounds_to_integer(self) -> None:
        """Float values in the salary range must be rounded to int."""
        normalizer = VacancyNormalizer()
        salary_from, salary_to = normalizer._extract_salary(  # noqa: SLF001
            {"from": 100001.6, "to": 200002.4, "currency": "RUR"}
        )
        assert salary_from == 100002
        assert salary_to == 200002

    def test_missing_salary_block_returns_none(self) -> None:
        normalizer = VacancyNormalizer()
        assert normalizer._extract_salary(None) == (None, None)  # noqa: SLF001
        assert normalizer._extract_salary({}) == (None, None)  # noqa: SLF001


# ---------------------------------------------------------------------------
# Dispatcher & unknown source
# ---------------------------------------------------------------------------


class TestDispatcher:
    def test_unknown_source_raises(self) -> None:
        with pytest.raises(NotImplementedError, match="habr"):
            VacancyNormalizer().normalize("habr", {"id": "1"})

    def test_normalize_alias_matches_normalize_hh(self, hh_raw: dict) -> None:
        from_dispatch = VacancyNormalizer().normalize("hh", hh_raw)
        from_method = VacancyNormalizer().normalize_hh(hh_raw)
        assert from_dispatch.source == from_method.source
        assert from_dispatch.source_id == from_method.source_id
        assert from_dispatch.salary_from == from_method.salary_from


# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------


class TestContentHashHelper:
    def test_returns_64_hex_chars(self) -> None:
        h = compute_content_hash("Title", "Desc", "Acme")
        assert len(h) == 64
        int(h, 16)  # parses as hex

    def test_changes_when_any_field_changes(self) -> None:
        base = compute_content_hash("Title", "Desc", "Acme")
        assert compute_content_hash("Other", "Desc", "Acme") != base
        assert compute_content_hash("Title", "Other", "Acme") != base
        assert compute_content_hash("Title", "Desc", "Other") != base

    def test_pipe_separator_prevents_collision(self) -> None:
        # Without the separator, ("ab", "c") and ("a", "bc") would hash the
        # same way. The pipe makes the boundary explicit.
        assert compute_content_hash("ab", "c", "") != compute_content_hash("a", "bc", "")

    def test_none_fields_become_empty_strings(self) -> None:
        h_none = compute_content_hash("T", None, None)
        h_empty = compute_content_hash("T", "", "")
        assert h_none == h_empty


class TestParseHhDatetime:
    def test_parses_with_colonless_timezone(self) -> None:
        dt = _parse_hh_datetime("2025-12-01T10:00:00+0300")
        assert dt is not None
        assert dt.tzinfo is not None
        assert dt.year == 2025 and dt.month == 12 and dt.day == 1
        # 10:00 +03:00 == 07:00 UTC
        assert dt.utcoffset().total_seconds() == 0  # normalised to UTC
        assert dt.hour == 7

    def test_returns_none_for_empty(self) -> None:
        assert _parse_hh_datetime(None) is None
        assert _parse_hh_datetime("") is None

    def test_returns_none_for_garbage(self) -> None:
        assert _parse_hh_datetime("not-a-date") is None

    def test_returns_aware_datetime(self) -> None:
        dt = _parse_hh_datetime("2025-12-01T10:00:00+0300")
        assert isinstance(dt, datetime)
        assert dt.tzinfo is not None
