"""Pydantic DTOs for the sources slice.

The HTTP layer and any future cross-slice integration talk in these
types; the ORM :class:`Vacancy` model never leaves the repository. The
DTOs use :class:`ConfigDict.from_attributes` so they can be built
straight from an ORM instance via ``VacancyRead.model_validate(row)``.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class VacancyRead(BaseModel):
    """Public serialisation of a :class:`Vacancy`.

    Mirrors the ORM model's columns 1:1. Optional fields stay
    optional (the same way the model treats them); a vacancy with no
    employer round-trip renders ``employer_name: null`` rather than
    dropping the key.

    ``raw_data`` is intentionally **excluded** from the public DTO: it
    is the original source payload and is only relevant inside the
    sources slice (dedup, re-normalisation). Exposing it would leak
    implementation details of the normaliser.
    """

    model_config = ConfigDict(extra="forbid", frozen=False, from_attributes=True)

    id: uuid.UUID
    source: str = Field(max_length=50)
    source_id: str = Field(max_length=255)
    title: str
    description: str | None = None
    url: str | None = None
    salary_from: int | None = None
    salary_to: int | None = None
    salary_currency: str
    salary_gross: bool
    employer_name: str | None = None
    location: str | None = None
    schedule: str | None = None
    experience: str | None = None
    skills: list[str] | None = None
    published_at: datetime | None = None
    source_updated_at: datetime | None = None
    created_at: datetime
    updated_at: datetime | None = None


class VacancyListResponse(BaseModel):
    """Response envelope for ``GET /vacancies``.

    The shape is a plain envelope (not a JSON:API object) so the
    dashboard can do ``body.items.map(...)`` without unwrapping a
    ``data``/``meta`` pair. ``total`` reflects the full match count
    *before* pagination, not just the current page.
    """

    model_config = ConfigDict(extra="forbid", frozen=False)

    items: list[VacancyRead]
    total: int = Field(ge=0, description="Total number of rows matching the filter set.")
    limit: int = Field(ge=1, le=100, description="The page size that was applied.")
    offset: int = Field(ge=0, description="The number of rows skipped.")


__all__ = [
    "VacancyListResponse",
    "VacancyRead",
]
