"""Pydantic schemas for the admin/integrations slice (M6, issue #57).

The :class:`IntegrationStatus` dataclass in
:mod:`job_apply.features.admin.integrations` is the in-process contract;
these Pydantic models are the wire format for the
``/admin/integrations`` endpoints. The conversion is mechanical and
goes through :func:`integration_status_to_read`.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class IntegrationStatusRead(BaseModel):
    """Wire format for a single integration health snapshot.

    ``from_attributes=True`` lets the API layer pass the dataclass
    straight to :meth:`model_validate`; the explicit
    :func:`integration_status_to_read` keeps the contract in one place.
    """

    model_config = ConfigDict(extra="forbid", frozen=False, from_attributes=True)

    name: str = Field(description="Stable integration identifier (e.g. 'hh', 'llm').")
    status: str = Field(
        description="One of 'healthy', 'degraded', 'unhealthy', 'unknown'.",
    )
    last_checked_at: datetime = Field(
        description="UTC wall-clock time of the most recent health check.",
    )
    error: str | None = Field(
        default=None,
        description="Human-readable error message; null on success.",
    )
    metadata: dict[str, Any] | None = Field(
        default=None,
        description="Free-form structured context (latency, status code, ...).",
    )


def integration_status_to_read(status: Any) -> IntegrationStatusRead:
    """Convert an :class:`IntegrationStatus` dataclass to a Pydantic model.

    The function is intentionally tiny: every field is copied 1:1
    and the ``metadata`` dict is shallow-copied so the API response
    cannot accidentally mutate the in-process state.
    """
    return IntegrationStatusRead(
        name=status.name,
        status=status.status,
        last_checked_at=status.last_checked_at,
        error=status.error,
        metadata=dict(status.metadata) if status.metadata is not None else None,
    )


__all__ = [
    "IntegrationStatusRead",
    "integration_status_to_read",
]
