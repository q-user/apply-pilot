"""Generic Pydantic helpers reused across vertical slices.

These mixins exist for two reasons only:

* to keep the DTO shape (an integer ``id``; ``created_at``/``updated_at``
  timestamps) consistent across slices, and
* to give slice-local schemas a single, well-known parent so that helpers
  like "give me the id of this DTO" work without isinstance gymnastics.

Anything more opinionated (enums of statuses, computed fields, validators)
belongs inside the slice.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


def _utcnow() -> datetime:
    """Return a timezone-aware ``datetime`` in UTC.

    Centralising this keeps tests and slices aligned when we eventually
    want to swap in a clock fake for time-sensitive tests.
    """
    return datetime.now(UTC)


class IdentifiedSchema(BaseModel):
    """Mixin for any DTO that carries a database-style integer identifier."""

    model_config = ConfigDict(extra="forbid", frozen=False)

    id: int = Field(ge=1, description="Stable, positive identifier of the resource.")


class TimestampedSchema(BaseModel):
    """Mixin for any DTO that tracks when it was created and last updated."""

    model_config = ConfigDict(extra="forbid", frozen=False)

    created_at: datetime = Field(
        default_factory=_utcnow,
        description="When the resource was first persisted.",
    )
    updated_at: datetime | None = Field(
        default=None,
        description=(
            "When the resource was last modified. ``None`` when the resource "
            "has not been updated since creation."
        ),
    )


def to_dict(model: BaseModel) -> dict[str, Any]:
    """Serialise a Pydantic model to a plain ``dict``.

    Provided as a single import point so call sites do not have to remember
    the v2 spelling (``model_dump`` vs ``dict``).
    """
    return model.model_dump()
