"""DTOs for the ``apply_worker`` slice (M5, issue #43).

The slice uses a single read DTO. Inputs are deliberately thin:
``ApplyJobService`` owns validation rules, ownership checks, and the
status-transition logic.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict

from job_apply.features.apply_worker.models import ApplyJob


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


__all__ = ["ApplyJobRead", "apply_job_to_dto"]
