"""DTOs for the search_profiles slice."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator


class SearchProfileCreate(BaseModel):
    """Input for ``POST /search-profiles``."""

    model_config = ConfigDict(extra="forbid", frozen=False)

    title: str = Field(min_length=1, max_length=255)
    keywords: str | None = Field(default=None, max_length=1024)
    salary_min: int | None = Field(default=None, ge=0)
    salary_max: int | None = Field(default=None, ge=0)
    location: str | None = Field(default=None, max_length=255)
    schedule: str | None = Field(default=None, max_length=255)

    @model_validator(mode="after")
    def validate_salary_range(self) -> SearchProfileCreate:
        if (
            self.salary_min is not None
            and self.salary_max is not None
            and self.salary_min > self.salary_max
        ):
            raise ValueError("salary_min must be <= salary_max")
        return self


class SearchProfileUpdate(BaseModel):
    """Input for ``PUT /search-profiles/{id}`` — all fields optional."""

    model_config = ConfigDict(extra="forbid", frozen=False)

    title: str | None = Field(default=None, min_length=1, max_length=255)
    keywords: str | None = Field(default=None, max_length=1024)
    salary_min: int | None = Field(default=None, ge=0)
    salary_max: int | None = Field(default=None, ge=0)
    location: str | None = Field(default=None, max_length=255)
    schedule: str | None = Field(default=None, max_length=255)
    is_active: bool | None = None

    @model_validator(mode="after")
    def validate_salary_range(self) -> SearchProfileUpdate:
        smin = self.salary_min
        smax = self.salary_max
        if smin is not None and smax is not None and smin > smax:
            raise ValueError("salary_min must be <= salary_max")
        return self


class SearchProfileRead(BaseModel):
    """Output shape for a search profile resource."""

    model_config = ConfigDict(extra="forbid", frozen=False)

    id: uuid.UUID
    user_id: uuid.UUID
    title: str
    keywords: str | None = None
    salary_min: int | None = None
    salary_max: int | None = None
    location: str | None = None
    schedule: str | None = None
    is_active: bool = True
    created_at: datetime
    updated_at: datetime | None = None


__all__ = [
    "SearchProfileCreate",
    "SearchProfileRead",
    "SearchProfileUpdate",
]
