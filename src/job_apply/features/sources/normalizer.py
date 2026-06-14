"""Vacancy normalizer — transforms raw source data into the canonical Vacancy model.

Each source has its own mapping logic. The dispatcher (`normalize`)
routes to the correct method based on the ``source`` argument.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime

from job_apply.features.sources.models import Vacancy

# Coefficient for converting gross salary to net (Russia: 13% tax → 87% net).
_GROSS_TO_NET_COEFFICIENT = 0.87

# Standard working hours per month for hourly→monthly conversion.
_HOURS_PER_MONTH = 168


def _compute_content_hash(
    title: str,
    description: str | None,
    employer_name: str | None,
) -> str:
    """Compute a SHA-256 hash of (title + description + employer_name)."""
    parts = [title, description or "", employer_name or ""]
    joined = "|".join(parts)
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()


def _parse_hh_datetime(raw: str | None) -> datetime | None:
    """Parse an hh.ru datetime string like '2025-12-01T10:00:00+0300'."""
    if not raw:
        return None
    try:
        # hh.ru uses +0300 format (no colon in timezone offset)
        dt = datetime.strptime(raw, "%Y-%m-%dT%H:%M:%S%z")
        return dt.astimezone(UTC)
    except (ValueError, TypeError):
        return None


class VacancyNormalizer:
    """Map raw source-specific payloads into the canonical Vacancy model.

    Usage::

        normalizer = VacancyNormalizer()
        vacancy = normalizer.normalize("hh", raw_api_response)
    """

    def normalize(self, source: str, raw: dict) -> Vacancy:
        """Dispatch to the source-specific normalizer."""
        if source == "hh":
            return self.normalize_hh(raw)
        raise NotImplementedError(f"No normalizer for source {source!r}")

    # ------------------------------------------------------------------
    # hh.ru
    # ------------------------------------------------------------------

    def normalize_hh(self, raw: dict) -> Vacancy:
        """Map an hh.ru /vacancies/{id} API response into a Vacancy."""
        salary_data = raw.get("salary") or {}
        salary_from, salary_to = self._extract_salary(salary_data)

        skills: list[str] = []
        for skill in raw.get("key_skills") or []:
            if isinstance(skill, dict) and skill.get("name"):
                skills.append(skill["name"])

        title = str(raw.get("name") or "")
        description = raw.get("description")
        employer_name = raw.get("employer", {}).get("name") if raw.get("employer") else None

        return Vacancy(
            source="hh",
            source_id=str(raw.get("id", "")),
            title=title,
            description=description,
            url=raw.get("url") or raw.get("alternate_url"),
            salary_from=salary_from,
            salary_to=salary_to,
            salary_currency=str(salary_data.get("currency", "RUR")),
            salary_gross=False,  # Stored values are always net after normalization
            employer_name=employer_name,
            location=raw.get("area", {}).get("name") if raw.get("area") else None,
            schedule=raw.get("schedule", {}).get("name") if raw.get("schedule") else None,
            experience=raw.get("experience", {}).get("name") if raw.get("experience") else None,
            skills=skills if skills else None,
            published_at=_parse_hh_datetime(raw.get("published_at")),
            raw_data=raw,
            content_hash=_compute_content_hash(title, description, employer_name),
        )

    # ------------------------------------------------------------------
    # Salary helpers
    # ------------------------------------------------------------------

    def _extract_salary(self, salary: dict | None) -> tuple[int | None, int | None]:
        """Extract and normalise salary_from, salary_to from raw salary dict.

        Applies:
        - hourly→monthly conversion (×168)
        - gross→net conversion (×0.87)

        Returns (salary_from, salary_to) as integers or None.
        """
        if not salary:
            return None, None

        raw_from = salary.get("from")
        raw_to = salary.get("to")

        # Convert hourly to monthly
        salary_type = salary.get("type", {})
        if isinstance(salary_type, dict) and salary_type.get("id") == "hourly":
            if raw_from is not None:
                raw_from = int(raw_from) * _HOURS_PER_MONTH
            if raw_to is not None:
                raw_to = int(raw_to) * _HOURS_PER_MONTH

        # Convert gross to net
        if salary.get("gross"):
            if raw_from is not None:
                raw_from = round(int(raw_from) * _GROSS_TO_NET_COEFFICIENT)
            if raw_to is not None:
                raw_to = round(int(raw_to) * _GROSS_TO_NET_COEFFICIENT)

        # Ensure integers
        salary_from = int(raw_from) if raw_from is not None else None
        salary_to = int(raw_to) if raw_to is not None else None

        return salary_from, salary_to


__all__ = ["VacancyNormalizer"]
