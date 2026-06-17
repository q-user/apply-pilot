"""Pydantic schemas for the source-metrics slice (M7, issue #62).

The :class:`SourceMetricEvent` dataclass in
:mod:`apply_pilot.features.source_metrics.models` is the in-process
contract; these Pydantic models are the wire format for
``GET /admin/sources/metrics``. The conversion is mechanical and
goes through :func:`source_metric_event_to_read`.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from apply_pilot.features.source_metrics.models import SourceMetricEvent


class SourceMetricRead(BaseModel):
    """Wire format for a single :class:`SourceMetricEvent`.

    ``from_attributes=True`` lets the API layer pass the dataclass
    straight to :meth:`model_validate`; the explicit
    :func:`source_metric_event_to_read` keeps the contract in one
    place and shallow-copies ``metadata`` so the API response
    cannot accidentally mutate the in-process state.
    """

    model_config = ConfigDict(extra="forbid", frozen=False, from_attributes=True)

    id: uuid.UUID = Field(description="Stable UUID4 for the event.")
    source_name: str = Field(description="Source identifier (e.g. 'hh', 'habr').")
    kind: str = Field(description="One of 'fetch', 'normalize', 'dedupe', 'fail'.")
    count: int = Field(description="Number of items the event refers to.")
    duration_ms: int = Field(description="Wall-clock duration of the ingest call in ms.")
    timestamp: datetime = Field(description="UTC wall-clock time the event was recorded.")
    metadata: dict[str, Any] = Field(
        default_factory=dict,
        description="Free-form structured context (counts, batch size, ...).",
    )


def source_metric_event_to_read(event: SourceMetricEvent) -> SourceMetricRead:
    """Convert a :class:`SourceMetricEvent` dataclass to a Pydantic model."""
    return SourceMetricRead(
        id=event.id,
        source_name=event.source_name,
        kind=event.kind.value,
        count=event.count,
        duration_ms=event.duration_ms,
        timestamp=event.timestamp,
        metadata=dict(event.metadata),
    )


__all__ = [
    "SourceMetricRead",
    "source_metric_event_to_read",
]
