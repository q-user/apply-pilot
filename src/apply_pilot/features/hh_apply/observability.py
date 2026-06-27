"""hh_apply observability — structured events + metrics + listener Protocol.

Source-of-truth contract: docs/integrations/hh_apply.md section 6 (T6 AC) + T6 #247.

Usage pattern (T5 wires):
    dispatcher = EventDispatcher(listeners=[log_listener(), prometheus_listener()])
    metrics = MetricsAccumulator()
    # ... around apply_once calls ...
    start = time.monotonic()
    result = await apply_once(request, *, client=client, retry_policy=rp)
    duration_ms = (time.monotonic() - start) * 1000
    dispatcher.emit(ApplyEvent(...))
    metrics.record(tenant_id=tenant_id, success=result.status == success, duration_ms=duration_ms,
                  retries=max(0, result.attempt_count - 1))
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import StrEnum

from .models import ApplyStatus

logger = logging.getLogger(__name__)


class EventType(StrEnum):
    """Structured event types emitted by hh_apply — see docs/integrations/hh_apply.md section 6."""

    ATTEMPT_STARTED = "apply_attempt_started"
    ATTEMPT_COMPLETED = "apply_attempt_completed"
    ATTEMPT_FAILED_RECOVERABLE = "apply_attempt_failed_recoverable"
    ATTEMPT_FAILED_UNRECOVERABLE = "apply_attempt_failed_unrecoverable"


@dataclass(frozen=True)
class ApplyEvent:
    """Structured event payload — carries tenant_id (None in OSS), vacancy_id, result fields.

    Per T6 AC every event carries tenant_id (None in OSS single-user mode).
    """

    event_type: EventType
    tenant_id: str | None
    vacancy_id: str
    resume_id: str
    status: ApplyStatus | None = None
    http_status: int | None = None
    attempt_count: int = 1
    duration_ms: float | None = None
    error_code: str | None = None
    negotiation_id: str | None = None


from typing import Protocol  # noqa: E402


class EventListener(Protocol):
    """Pluggable sink surface. Implementations must NOT raise — EventDispatcher
    catches and logs (defense-in-depth: a listener that crashes the apply pipeline
    is an operator's nightmare).
    """

    def on_event(self, event: ApplyEvent) -> None: ...


class EventDispatcher:
    """Multi-listener fan-out with per-listener exception isolation.

    T5 (#246) constructs this at worker startup, attaches its observers
    (logging, Prometheus, OpenTelemetry, etc.), and calls `emit(...)` around
    every `apply_once` lifecycle boundary.
    """

    def __init__(self, listeners: list[EventListener] | None = None) -> None:
        self._listeners: list[EventListener] = list(listeners) if listeners else []

    def attach(self, listener: EventListener) -> None:
        self._listeners.append(listener)

    def emit(self, event: ApplyEvent) -> None:
        # Forensic visibility via stdlib logging at debug level regardless of listener state
        logger.debug(
            "hh_apply.event: %s tenant=%s vacancy=%s resume=%s status=%s http=%s "
            "attempts=%s duration_ms=%s error=%s neg_id=%s",
            event.event_type.value,
            event.tenant_id,
            event.vacancy_id,
            event.resume_id,
            event.status.value if event.status is not None else None,
            event.http_status,
            event.attempt_count,
            event.duration_ms,
            event.error_code,
            event.negotiation_id,
        )
        for listener in list(self._listeners):  # defensive copy; tolerate attach() during emit
            try:
                listener.on_event(event)
            except Exception as exc:  # noqa: BLE001 — listeners must NOT break the apply pipeline
                logger.warning("hh_apply.event: listener %r raised %s; continuing", listener, exc)


@dataclass
class MetricsSnapshot:
    """Materialized per-tenant metrics view at a point in time."""

    attempts: int = 0
    successes: int = 0
    failures: int = 0
    latency_ms_sum: float = 0.0
    latency_ms_count: int = 0
    retries_total: int = 0

    @property
    def success_rate(self) -> float:
        return (self.successes / self.attempts) if self.attempts else 0.0

    @property
    def avg_latency_ms(self) -> float:
        return (self.latency_ms_sum / self.latency_ms_count) if self.latency_ms_count else 0.0


class MetricsAccumulator:
    """In-memory counter + histogram per tenant (None = OSS single-user mode).

    No external metric library dep — T5 may layer a Prometheus exporter on top
    via the `snapshot()` API; T6 itself ships only the accumulator.
    """

    def __init__(self) -> None:
        self._by_tenant: dict[str | None, MetricsSnapshot] = {}

    def record(
        self,
        *,
        tenant_id: str | None,
        success: bool,
        duration_ms: float,
        retries: int = 0,
    ) -> None:
        if duration_ms < 0:
            raise ValueError(f"duration_ms must be >= 0; got {duration_ms}")
        if retries < 0:
            raise ValueError(f"retries must be >= 0; got {retries}")
        snap = self._by_tenant.get(tenant_id)
        if snap is None:
            snap = MetricsSnapshot()
            self._by_tenant[tenant_id] = snap
        snap.attempts += 1
        snap.latency_ms_sum += duration_ms
        snap.latency_ms_count += 1
        snap.retries_total += retries
        if success:
            snap.successes += 1
        else:
            snap.failures += 1

    def snapshot(self):
        """Return read-only view (MappingProxyType) of per-tenant metrics — callers
        cannot mutate our state nor the snapshot it returns."""
        from types import MappingProxyType

        return MappingProxyType(
            {
                tenant_id: MetricsSnapshot(
                    attempts=s.attempts,
                    successes=s.successes,
                    failures=s.failures,
                    latency_ms_sum=s.latency_ms_sum,
                    latency_ms_count=s.latency_ms_count,
                    retries_total=s.retries_total,
                )
                for tenant_id, s in self._by_tenant.items()
            }
        )
