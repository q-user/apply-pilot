"""sources — ingest and normalise vacancies from external job boards."""

from __future__ import annotations

from job_apply.features.sources.dedup import VacancyDeduplicator
from job_apply.features.sources.models import Vacancy
from job_apply.features.sources.normalizer import VacancyNormalizer
from job_apply.features.sources.repository import (
    InMemoryVacancyRepository,
    SqlVacancyRepository,
    VacancyRepository,
)
from job_apply.features.sources.service import SourceService

__all__ = [
    "InMemoryVacancyRepository",
    "SourceService",
    "SqlVacancyRepository",
    "Vacancy",
    "VacancyDeduplicator",
    "VacancyNormalizer",
    "VacancyRepository",
]
