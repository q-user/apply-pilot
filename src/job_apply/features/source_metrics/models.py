"""Models for the source-metrics slice (M7, issue #62).

Two model types live in this module:

* :class:`SourceMetricEventKind` — the four closed-set enums the
  service records per ingest call (:attr:`FETCH`,
  :attr:`NORMALIZE`, :attr:`DEDUPE`, :attr:`FAIL`).
* :class:`SourceMetricEvent` — the immutable, in-memory value object
  the service hands to the repository. Built as a ``frozen=True``
  dataclass so the record cannot be mutated after the service
  observes it.
* :class:`SourceMetricEventORM` — the SQLAlchemy ORM row the SQL
  repository writes to. Mirrors :class:`SourceMetricEvent` field
  for field; the ``metadata`` field is serialised as JSON-encoded
  ``TEXT`` to keep the migration portable across sqlite and
  PostgreSQL (same convention as :class:`CoverLetterStyle`).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import DateTime, Index, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from job_apply.db import Base
from job_apply.shared.types import GUID


class SourceMetricEventKind(StrEnum):
    """Closed set of metric kinds the source-metrics slice records.

    The set is intentionally small: the issue spec lists exactly the
    four kinds an operator wants to plot per source. Adding a new
    kind is a public-API change for the slice — the data model, the
    API DTO, and every downstream consumer must agree.
    """

    FETCH = "fetch"
    NORMALIZE = "normalize"
    DEDUPE = "dedupe"
    FAIL = "fail"


@dataclass(frozen=True, slots=True)
class SourceMetricEvent:
    """A single metric event recorded for an ingest call.

    The four counts (``fetched``, ``normalized``, ``deduped``,
    ``failed``) and the ``duration_ms`` are passed to the service
    once per ingest call; the service writes one event per kind with
    the same ``timestamp`` and ``duration_ms`` so the four rows can
    be correlated by their ``timestamp`` (and the
    ``source_name`` + ``timestamp`` natural key).

    Attributes
    ----------
    source_name:
        Stable source identifier (``"hh"``, ``"habr"``,
        ``"telegram"``, ...). Indexable.
    kind:
        One of the :class:`SourceMetricEventKind` values.
    count:
        How many items this event refers to. ``0`` is a valid value
        — "no rows failed" is itself a data point the operator
        wants to see.
    duration_ms:
        Wall-clock duration of the ingest call in milliseconds. The
        same value is shared by the four events a single ingest
        produces; the service reads it from
        :func:`time.monotonic` and converts to ms.
    timestamp:
        UTC wall-clock time the event was recorded. The service
        pulls this from an injectable ``clock`` callable so tests
        can pin the value.
    metadata:
        Free-form structured context (counts of every kind, batch
        size, ...). Stored as JSON in the SQL backend. ``{}`` when
        nothing else is relevant.
    id:
        UUID4 generated on construction. The field is the natural
        primary key the SQL repository writes to.
    """

    source_name: str
    kind: SourceMetricEventKind
    count: int
    duration_ms: int
    timestamp: datetime
    metadata: dict[str, Any] = field(default_factory=dict)
    id: uuid.UUID = field(default_factory=uuid.uuid4)


class SourceMetricEventORM(Base):
    """SQLAlchemy ORM row backing :class:`SourceMetricEvent`.

    Schema
    ------

    * ``id``            — UUID primary key.
    * ``source_name``   — stable source identifier; indexed.
    * ``kind``          — one of :class:`SourceMetricEventKind`.
    * ``count``         — items the event refers to.
    * ``duration_ms``   — wall-clock duration of the ingest call.
    * ``timestamp``     — UTC wall-clock time the event was recorded.
    * ``metadata_json`` — JSON-encoded free-form context.

    Indexes
    -------

    * ``ix_source_metric_events_source_name_timestamp`` composite on
      ``(source_name, timestamp)`` backs
      :meth:`SqlSourceMetricRepository.query` and keeps the per-source
      timeline query cheap as the table grows.
    """

    __tablename__ = "source_metric_events"
    __table_args__ = (
        Index(
            "ix_source_metric_events_source_name_timestamp",
            "source_name",
            "timestamp",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=uuid.uuid4)
    source_name: Mapped[str] = mapped_column(String(50), nullable=False)
    kind: Mapped[str] = mapped_column(String(20), nullable=False)
    count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    duration_ms: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    timestamp: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    metadata_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return (
            f"SourceMetricEventORM(id={self.id!s}, source_name={self.source_name!r}, "
            f"kind={self.kind!r}, count={self.count!r}, duration_ms={self.duration_ms!r})"
        )


def _utcnow() -> datetime:
    """Return the current UTC time. Wrapped so tests can monkey-patch it."""
    return datetime.now(UTC)


__all__ = [
    "SourceMetricEvent",
    "SourceMetricEventKind",
    "SourceMetricEventORM",
]
