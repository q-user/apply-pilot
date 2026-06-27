"""Pydantic models for the hh_apply slice — request, result, error, status enum, exception."""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict


class ApplyStatus(StrEnum):
    """Terminal status values for `apply_once` — see docs/integrations/hh_apply.md §1."""

    success = "success"
    idle_already_applied = "idle_already_applied"
    validation_error = "validation_error"
    auth_required = "auth_required"
    rate_limited = "rate_limited"
    upstream_error = "upstream_error"


class ApplyRequest(BaseModel):
    """Apply payload from apply_worker.dispatch → hh_apply.apply_once — see doc §4."""

    model_config = ConfigDict(frozen=True)

    vacancy_id: str
    resume_id: str
    message: str
    lux: bool = False
    force: bool = False  # T5 (worker integration) idempotency override


class ApplyError(BaseModel):
    """Structured error — carries enough for diagnostics + T6 observability events."""

    code: str
    message: str
    http_status: int
    raw: dict[str, Any] | None = None


class ApplyResult(BaseModel):
    """Final return contract — see doc §1 status table."""

    status: ApplyStatus
    negotiation_id: str | None = None
    http_status: int
    raw: dict[str, Any] | None = None
    attempt_count: int = 1
    error: ApplyError | None = None


class HHApplyError(Exception):
    """Unrecoverable condition: invalid request, session truly dead after refresh, etc."""
