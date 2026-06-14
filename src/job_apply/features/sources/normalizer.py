"""Vacancy normaliser.

A :class:`VacancyNormalizer` turns raw payloads from external job boards
into the canonical :class:`Vacancy` model. Each source has its own mapping
rules; the top-level :meth:`VacancyNormalizer.normalize` dispatches to the
right method based on the ``source`` argument.

Currently supported sources
---------------------------

* ``hh`` — hh.ru ``/vacancies/{id}`` API response (see
  :meth:`VacancyNormalizer.normalize_hh`).

Salary normalisation
--------------------

Salaries in the hh.ru payload can be in two non-canonical forms:

* ``salary.gross == true`` — the amount is *before* personal income tax
  (13 % in Russia). We convert to net by multiplying by 0.87.
* ``salary.type.id == "hourly"`` — the amount is per hour. We convert to
  a monthly figure by multiplying by 168 (the conventional month-hour
  count).

The two conversions compose: an hourly, gross 300 ₽/h rate is normalised
to ``300 * 168 * 0.87 ≈ 43 848`` ₽/month net.

After conversion, both endpoints are rounded to integers and the stored
``salary_gross`` flag is always ``False`` (the model only stores net).
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from typing import Any, Final

from job_apply.features.sources.models import Vacancy

#: Coefficient applied to gross salaries to obtain the net equivalent
#: (Russia: PIT = 13 % → 0.87 retention).
_GROSS_TO_NET_COEFFICIENT: Final[float] = 0.87

#: Standard full-time working hours per month, used for hourly→monthly
#: conversion (8 h × 21 working days).
_HOURS_PER_MONTH: Final[int] = 168


# ---------------------------------------------------------------------------
# Module-level helpers (pure functions, easy to unit test in isolation)
# ---------------------------------------------------------------------------


def _compute_content_hash(
    title: str,
    description: str | None,
    employer_name: str | None,
) -> str:
    """Return the SHA-256 hex digest of ``title|description|employer_name``.

    The pipe separator is included to avoid a contrived collision where
    ``("ab", "c")`` and ``("a", "bc")`` would otherwise hash the same.
    """
    parts = [title, description or "", employer_name or ""]
    joined = "|".join(parts)
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()


def _parse_hh_datetime(raw: str | None) -> datetime | None:
    """Parse an hh.ru ISO-8601 timestamp like ``2025-12-01T10:00:00+0300``.

    Returns a timezone-aware ``datetime`` in UTC, or ``None`` if the input
    is missing or unparseable. hh.ru omits the colon in the timezone
    offset, which :func:`datetime.fromisoformat` only accepts in
    Python 3.11+; we use :func:`datetime.strptime` for portability.
    """
    if not raw:
        return None
    try:
        dt = datetime.strptime(raw, "%Y-%m-%dT%H:%M:%S%z")
    except (TypeError, ValueError):
        return None
    return dt.astimezone(UTC)


def _coerce_int(value: Any) -> int | None:
    """Return ``int(value)`` if ``value`` is a number-like, else ``None``.

    Floats are rounded (banker's rounding) before truncation so the
    downstream integer columns do not silently lose precision from
    fractional salary figures. ``bool`` is rejected explicitly because
    Python's ``isinstance(True, int)`` is ``True`` and we do not want
    boolean truthiness sneaking into the salary columns.
    """
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, float):
        return int(round(value))
    if isinstance(value, int):
        return int(value)
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            try:
                return int(round(float(value)))
            except ValueError:
                return None
    return None


# ---------------------------------------------------------------------------
# Normaliser
# ---------------------------------------------------------------------------


class VacancyNormalizer:
    """Map raw source-specific payloads into the canonical :class:`Vacancy`."""

    def normalize(self, source: str, raw: dict[str, Any]) -> Vacancy:
        """Dispatch to the source-specific normaliser.

        Raises:
            NotImplementedError: if no normaliser is registered for ``source``.
        """
        if source == "hh":
            return self.normalize_hh(raw)
        raise NotImplementedError(
            f"No normaliser registered for source {source!r}. Known sources: ['hh']."
        )

    # ------------------------------------------------------------------
    # hh.ru
    # ------------------------------------------------------------------

    def normalize_hh(self, raw: dict[str, Any]) -> Vacancy:
        """Map an hh.ru vacancy payload into a canonical :class:`Vacancy`."""
        salary_data = raw.get("salary") or {}
        salary_from, salary_to = self._extract_salary(salary_data)

        skills: list[str] = []
        for skill in raw.get("key_skills") or []:
            if isinstance(skill, dict):
                name = skill.get("name")
                if name:
                    skills.append(str(name))

        employer = raw.get("employer") or {}
        area = raw.get("area") or {}
        schedule = raw.get("schedule") or {}
        experience = raw.get("experience") or {}

        title = str(raw.get("name") or "")
        description = raw.get("description")
        employer_name = employer.get("name") if isinstance(employer, dict) else None
        url = raw.get("url") or raw.get("alternate_url")

        return Vacancy(
            source="hh",
            source_id=str(raw.get("id", "")),
            title=title,
            description=description,
            url=url,
            salary_from=salary_from,
            salary_to=salary_to,
            salary_currency=str(salary_data.get("currency", "RUR"))
            if isinstance(salary_data, dict)
            else "RUR",
            # The stored values are always net after normalisation.
            salary_gross=False,
            employer_name=employer_name,
            location=area.get("name") if isinstance(area, dict) else None,
            schedule=schedule.get("name") if isinstance(schedule, dict) else None,
            experience=experience.get("name") if isinstance(experience, dict) else None,
            skills=skills or None,
            published_at=_parse_hh_datetime(raw.get("published_at")),
            raw_data=dict(raw),
            content_hash=_compute_content_hash(title, description, employer_name),
        )

    # ------------------------------------------------------------------
    # Salary helpers
    # ------------------------------------------------------------------

    def _extract_salary(self, salary: dict[str, Any] | None) -> tuple[int | None, int | None]:
        """Normalise ``salary.from`` / ``salary.to`` to net, monthly integers.

        Returns:
            A ``(salary_from, salary_to)`` tuple of integers, with ``None``
            for any missing endpoint. Both conversions are applied in
            order: hourly → monthly first (so the gross coefficient is
            applied to the already-monthly figure), then gross → net.
        """
        if not isinstance(salary, dict) or not salary:
            return None, None

        raw_from = _coerce_int(salary.get("from"))
        raw_to = _coerce_int(salary.get("to"))

        # Hourly → monthly.
        salary_type = salary.get("type")
        if isinstance(salary_type, dict) and salary_type.get("id") == "hourly":
            if raw_from is not None:
                raw_from = raw_from * _HOURS_PER_MONTH
            if raw_to is not None:
                raw_to = raw_to * _HOURS_PER_MONTH

        # Gross → net.
        if salary.get("gross"):
            if raw_from is not None:
                raw_from = round(raw_from * _GROSS_TO_NET_COEFFICIENT)
            if raw_to is not None:
                raw_to = round(raw_to * _GROSS_TO_NET_COEFFICIENT)

        return raw_from, raw_to


__all__ = ["VacancyNormalizer"]
