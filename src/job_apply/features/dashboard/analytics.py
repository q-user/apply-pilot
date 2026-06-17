"""Analytics value objects for the dashboard slice (M8, issue #67).

Three read-only aggregations live here:

* :class:`FunnelRow` — one row per ``source`` with five counts:
  ``fetched`` (Vacancy rows), ``matched`` (VacancyMatch rows),
  ``accepted`` (VacancyMatch with ``status='accepted'``), ``applied``
  (ApplyJob with a terminal state) and ``rejected`` (VacancyMatch with
  ``status='rejected'``).
* :class:`ConversionRow` — one row per search profile with the match,
  accept and apply counts, plus ``accepted_rate = accepted / matches``
  and ``applied_rate = applied / accepted`` (both default to ``0.0`` on
  zero denominators).
* :class:`TimeToApplyStats` — average and median wall-clock seconds from
  :attr:`VacancyMatch.created_at` to :attr:`ApplyJob.finished_at` for
  terminal-state jobs. ``None`` when no data is available.

The dataclasses are the in-process contract. The Pydantic models in
:mod:`job_apply.features.dashboard.schemas` mirror the same shape with
``from_attributes=True`` so the API layer can hand the dataclass
straight to ``model_validate``.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass


@dataclass(frozen=True)
class FunnelRow:
    """One row of the source funnel.

    Attributes:
        source: The ``vacancies.source`` value these counts are for.
        fetched: Number of :class:`Vacancy` rows for this source.
        matched: Number of :class:`VacancyMatch` rows whose
            :attr:`VacancyMatch.vacancy_id` points at a
            :class:`Vacancy` from this source.
        accepted: Number of :class:`VacancyMatch` rows with
            ``status='accepted'`` for this source.
        applied: Number of :class:`ApplyJob` rows with a terminal
            state (``succeeded``, ``failed``, ``dead_letter`` or
            ``cancelled``) for this source.
        rejected: Number of :class:`VacancyMatch` rows with
            ``status='rejected'`` for this source.
    """

    source: str
    fetched: int
    matched: int
    accepted: int
    applied: int
    rejected: int


@dataclass(frozen=True)
class ConversionRow:
    """One row of the per-profile conversion table.

    Attributes:
        profile_id: The :class:`SearchProfile` these counts are for.
        matches: Number of :class:`VacancyMatch` rows for the profile.
        accepted: Number of matches with ``status='accepted'``.
        applied: Number of :class:`ApplyJob` rows with a terminal
            state for matches owned by the profile.
        accepted_rate: ``accepted / matches`` (zero-safe).
        applied_rate: ``applied / accepted`` (zero-safe).
    """

    profile_id: uuid.UUID
    matches: int
    accepted: int
    applied: int
    accepted_rate: float
    applied_rate: float


@dataclass(frozen=True)
class TimeToApplyStats:
    """Average + median wall-clock time from match to applied.

    Attributes:
        average_seconds: Mean of ``apply_job.finished_at -
            match.created_at`` across the filtered set, in seconds.
        median_seconds: Median of the same delta, in seconds.
        sample_size: Number of ``(match, apply_job)`` pairs that
            contributed to the metric.
    """

    average_seconds: float
    median_seconds: float
    sample_size: int


__all__ = ["ConversionRow", "FunnelRow", "TimeToApplyStats"]
