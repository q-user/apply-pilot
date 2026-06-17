"""TDD tests for :class:`SourceMetricEvent` and the
:class:`SourceMetricEventKind` enum.

The :class:`SourceMetricEvent` is the immutable value object the
:mod:`source_metrics` slice records per ingest call. The tests below
pin the field set, the immutability guarantee, the enum values, and
the timestamp default behaviour.
"""

from __future__ import annotations

import dataclasses
import uuid
from datetime import UTC, datetime

import pytest

from job_apply.features.source_metrics.models import (
    SourceMetricEvent,
    SourceMetricEventKind,
)


def test_kind_enum_has_four_required_values() -> None:
    """The four kinds the issue spec lists must exist on the enum."""
    assert SourceMetricEventKind.FETCH == "fetch"
    assert SourceMetricEventKind.NORMALIZE == "normalize"
    assert SourceMetricEventKind.DEDUPE == "dedupe"
    assert SourceMetricEventKind.FAIL == "fail"

    # The set is intentionally closed: adding a new kind is a public-API
    # change for the metrics slice.
    assert {k.value for k in SourceMetricEventKind} == {"fetch", "normalize", "dedupe", "fail"}


def test_event_carries_documented_fields() -> None:
    """An event stores source_name, kind, count, duration_ms, timestamp, metadata."""
    ts = datetime(2026, 6, 17, 12, 0, 0, tzinfo=UTC)
    event = SourceMetricEvent(
        source_name="hh",
        kind=SourceMetricEventKind.FETCH,
        count=42,
        duration_ms=1234,
        timestamp=ts,
        metadata={"batch_size": 42},
    )

    assert event.source_name == "hh"
    assert event.kind == SourceMetricEventKind.FETCH
    assert event.count == 42
    assert event.duration_ms == 1234
    assert event.timestamp == ts
    assert event.metadata == {"batch_size": 42}


def test_event_is_immutable() -> None:
    """Mutating any field must raise — :class:`dataclasses` ``frozen=True``."""
    event = SourceMetricEvent(
        source_name="hh",
        kind=SourceMetricEventKind.FETCH,
        count=1,
        duration_ms=10,
        timestamp=datetime.now(UTC),
    )
    with pytest.raises(dataclasses.FrozenInstanceError):
        event.count = 999  # type: ignore[misc]


def test_event_id_is_populated_by_default() -> None:
    """The ``id`` field defaults to a fresh UUID4 when not supplied."""
    event = SourceMetricEvent(
        source_name="hh",
        kind=SourceMetricEventKind.FETCH,
        count=1,
        duration_ms=10,
        timestamp=datetime.now(UTC),
    )
    assert event.id is not None
    # Two separately-constructed events must not share an id.
    event_b = SourceMetricEvent(
        source_name="hh",
        kind=SourceMetricEventKind.FETCH,
        count=1,
        duration_ms=10,
        timestamp=datetime.now(UTC),
    )
    assert event.id != event_b.id
    # And the id must look like a UUID.
    uuid.UUID(str(event.id))


def test_event_metadata_defaults_to_empty_dict() -> None:
    """Omitting ``metadata`` must default to an empty dict, not ``None``."""
    event = SourceMetricEvent(
        source_name="hh",
        kind=SourceMetricEventKind.FETCH,
        count=1,
        duration_ms=10,
        timestamp=datetime.now(UTC),
    )
    assert event.metadata == {}
