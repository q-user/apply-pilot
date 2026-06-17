"""Source-metrics use-case service (M7, issue #62).

The :class:`SourceMetricsService` is the high-level facade
:class:`SourceService` calls once per ingest invocation. The
service translates the four counts (``fetched``, ``normalized``,
``deduped``, ``failed``) plus ``duration_ms`` into four
:class:`SourceMetricEvent` rows (one per :class:`SourceMetricEventKind`)
and hands them to the repository.

The service is intentionally fire-and-forget: the API layer never
reads metrics synchronously, so a slow or failing repository
should not block the ingest pipeline. We do not catch
:class:`Exception` here — the caller (a worker or a request
handler) is expected to log and move on. The repository write is
the only side effect.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from job_apply.features.source_metrics.models import (
    SourceMetricEvent,
    SourceMetricEventKind,
)
from job_apply.features.source_metrics.repository import SourceMetricRepository

_LOGGER = logging.getLogger(__name__)


def _default_clock() -> datetime:
    """Return the current UTC time. Wrapped so tests can inject a clock."""
    return datetime.now(UTC)


class SourceMetricsService:
    """Record per-source ingest metrics.

    The service is constructed with a :class:`SourceMetricRepository`
    and an optional ``clock`` callable (defaults to
    :func:`datetime.now(UTC)`). The clock is injected so the unit
    tests can pin the timestamp without monkey-patching the module.

    Public surface
    --------------

    * :meth:`record_ingest` — write the four event kinds for one
      ingest call. The four events share a single ``timestamp``
      and ``duration_ms`` so a downstream analyst can correlate
      them by their natural key.
    """

    def __init__(
        self,
        *,
        metric_repo: SourceMetricRepository,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._metric_repo = metric_repo
        self._clock = clock or _default_clock

    def record_ingest(
        self,
        *,
        source_name: str,
        fetched: int,
        normalized: int,
        deduped: int,
        failed: int,
        duration_ms: int,
        metadata: dict[str, Any] | None = None,
    ) -> list[SourceMetricEvent]:
        """Record the four event kinds for one ingest invocation.

        All four events share the same ``timestamp`` (the call time)
        and ``duration_ms`` (the wall-clock duration of the ingest
        call). The ``metadata`` dict is mirrored into every event so
        ad-hoc SQL queries can ``GROUP BY`` any of the four counts
        without joining against the event row's primary key.

        The method returns the four events in insertion order
        (FETCH, NORMALIZE, DEDUPE, FAIL) for tests that want to
        assert on the exact set; production callers ignore the
        return value.
        """
        timestamp = self._clock()
        base_metadata: dict[str, Any] = {
            "fetched": fetched,
            "normalized": normalized,
            "deduped": deduped,
            "failed": failed,
            "duration_ms": duration_ms,
        }
        if metadata:
            base_metadata.update(metadata)

        recorded: list[SourceMetricEvent] = []
        for kind, count in (
            (SourceMetricEventKind.FETCH, fetched),
            (SourceMetricEventKind.NORMALIZE, normalized),
            (SourceMetricEventKind.DEDUPE, deduped),
            (SourceMetricEventKind.FAIL, failed),
        ):
            event = SourceMetricEvent(
                source_name=source_name,
                kind=kind,
                count=count,
                duration_ms=duration_ms,
                timestamp=timestamp,
                metadata=dict(base_metadata),
            )
            try:
                self._metric_repo.record(event)
            except Exception:
                _LOGGER.exception(
                    "source_metrics.record.failed",
                    extra={
                        "event": "source_metrics.record.failed",
                        "source_name": source_name,
                        "kind": kind.value,
                    },
                )
            recorded.append(event)
        return recorded

    @property
    def metric_repo(self) -> SourceMetricRepository:
        """Expose the repository for tests that need to assert state."""
        return self._metric_repo


__all__ = ["SourceMetricsService"]
