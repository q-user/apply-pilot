"""DTOs for the ``apply_worker`` slice (M5, issue #43).

The slice exposes three read DTOs: the apply-job view used by the
dashboard, the append-only history view (M5, issue #49) used to debug
failed runs and render the per-job timeline, and the rate-limit
snapshot (M5, issue #46) returned by ``GET /apply-jobs/limits``.
Inputs are deliberately thin: ``ApplyJobService`` owns validation
rules, ownership checks, and the status-transition logic.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, field_validator

from job_apply.features.apply_worker.limits import RateLimitResult, WindowStatus
from job_apply.features.apply_worker.models import (
    ApplyJob,
    ApplyStatusHistory,
)


class ApplyJobRead(BaseModel):
    """Public read shape for an :class:`ApplyJob`."""

    model_config = ConfigDict(extra="forbid", frozen=False, from_attributes=True)

    id: uuid.UUID
    match_id: uuid.UUID
    user_id: uuid.UUID
    vacancy_id: uuid.UUID
    status: str
    attempts: int
    last_error: str | None = None
    next_run_at: datetime | None = None
    external_application_id: str | None = None
    created_at: datetime
    updated_at: datetime | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None


def apply_job_to_dto(job: ApplyJob) -> ApplyJobRead:
    """Map an :class:`ApplyJob` ORM row to the public DTO."""
    return ApplyJobRead.model_validate(job)


class ApplyStatusHistoryRead(BaseModel):
    """Public read shape for an :class:`ApplyStatusHistory` row (M5, #49).

    The ``metadata`` field is exposed as a parsed dict; the underlying
    column stores JSON-encoded text. When the row has no metadata the
    field is ``None`` (mirroring the ``metadata_json`` column's
    nullability rather than defaulting to an empty object).
    """

    model_config = ConfigDict(extra="forbid", frozen=False, from_attributes=True)

    id: uuid.UUID
    job_id: uuid.UUID
    from_status: str | None
    to_status: str
    error: str | None = None
    metadata: dict[str, Any] | None = None
    created_at: datetime

    @field_validator("metadata", mode="before")
    @classmethod
    def _coerce_metadata(cls, value: Any) -> Any:
        """Accept either a parsed dict or the raw JSON-encoded string.

        ``from_attributes=True`` pulls the field straight off the ORM
        row, so the validator must handle the raw ``metadata_json``
        string the column stores. Validators run before model
        validation, so we decode here and let Pydantic validate the
        resulting ``dict[str, Any]`` shape.
        """
        if value is None or isinstance(value, dict):
            return value
        if isinstance(value, str):
            import json

            try:
                return json.loads(value)
            except json.JSONDecodeError:
                # An unparseable payload is preserved as a raw string
                # so the dashboard can still surface it.
                return {"raw": value}
        return value


def apply_status_history_to_dto(row: ApplyStatusHistory) -> ApplyStatusHistoryRead:
    """Map an :class:`ApplyStatusHistory` ORM row to the public DTO.

    The ORM row's ``metadata_json`` column is renamed to ``metadata``
    in the DTO; ``field_validator`` decodes the JSON string when
    needed.
    """
    payload: dict[str, Any] = {
        "id": row.id,
        "job_id": row.job_id,
        "from_status": row.from_status,
        "to_status": row.to_status,
        "error": row.error,
        "metadata": row.metadata_json,
        "created_at": row.created_at,
    }
    return ApplyStatusHistoryRead.model_validate(payload)


# ---------------------------------------------------------------------------
# Rate-limit snapshot (M5, issue #46)
# ---------------------------------------------------------------------------


class WindowStatusRead(BaseModel):
    """Public read shape for a single :class:`WindowStatus` snapshot.

    The DTO mirrors the dataclass 1:1; the conversion is done by
    :func:`window_status_to_dto` so the API contract stays in the
    schemas module and the dataclass stays free of Pydantic
    concerns.
    """

    model_config = ConfigDict(extra="forbid", frozen=False, from_attributes=True)

    used: int
    limit: int
    remaining: int
    reset_at: datetime | None = None


class ApplyRateLimitRead(BaseModel):
    """Public read shape for the ``GET /apply-jobs/limits`` endpoint.

    The endpoint returns a snapshot for each tracked window so the
    dashboard can render "10 of 30 used" / "back in 5 minutes"
    without doing the math itself.
    """

    model_config = ConfigDict(extra="forbid", frozen=False, from_attributes=True)

    hourly: WindowStatusRead
    daily: WindowStatusRead


def _window_status_to_dto(window: WindowStatus) -> WindowStatusRead:
    return WindowStatusRead(
        used=window.used,
        limit=window.limit,
        remaining=window.remaining,
        reset_at=window.reset_at,
    )


def apply_rate_limit_to_dto(result: RateLimitResult) -> ApplyRateLimitRead:
    """Map a :class:`RateLimitResult` to the public DTO."""
    return ApplyRateLimitRead(
        hourly=_window_status_to_dto(result.hourly),
        daily=_window_status_to_dto(result.daily),
    )


__all__ = [
    "ApplyJobRead",
    "ApplyRateLimitRead",
    "ApplyStatusHistoryRead",
    "WindowStatusRead",
    "apply_job_to_dto",
    "apply_rate_limit_to_dto",
    "apply_status_history_to_dto",
]
