"""EventDispatcher + MetricsAccumulator — exception-safe listeners + snapshot."""

from __future__ import annotations

import pytest

from apply_pilot.features.hh_apply.observability import (
    ApplyEvent,
    EventDispatcher,
    EventType,
    MetricsAccumulator,
    MetricsSnapshot,
)


def _evt(tenant_id=None, **kwargs) -> ApplyEvent:
    base = {
        "event_type": EventType.ATTEMPT_STARTED,
        "tenant_id": tenant_id,
        "vacancy_id": "v1",
        "resume_id": "r1",
    }
    base.update(kwargs)
    return ApplyEvent(**base)


class TestEventDispatcher:
    def test_listener_receives_event(self) -> None:
        captured: list[ApplyEvent] = []

        class L:
            def on_event(self, e: ApplyEvent) -> None:
                captured.append(e)

        d = EventDispatcher(listeners=[L()])
        d.emit(_evt())
        assert len(captured) == 1
        assert captured[0].vacancy_id == "v1"

    def test_attaching_late_listener_adds_to_fanout(self) -> None:
        captured: list[ApplyEvent] = []

        class L:
            def on_event(self, e: ApplyEvent) -> None:
                captured.append(e)

        d = EventDispatcher()
        d.attach(L())
        d.emit(_evt())
        assert len(captured) == 1

    def test_listener_raising_does_not_break_dispatch(self) -> None:
        captured: list[ApplyEvent] = []

        class Bad:
            def on_event(self, e: ApplyEvent) -> None:
                raise RuntimeError("listener exploded")

        class Good:
            def on_event(self, e: ApplyEvent) -> None:
                captured.append(e)

        d = EventDispatcher(listeners=[Bad(), Good()])
        # Must not raise — the whole point of the §6.3 contract
        d.emit(_evt())
        assert len(captured) == 1


class TestMetricsAccumulator:
    def test_record_attempt_increments(self) -> None:
        m = MetricsAccumulator()
        m.record(tenant_id=None, success=True, duration_ms=100.0, retries=0)
        m.record(tenant_id=None, success=False, duration_ms=200.0, retries=1)
        snap = m.snapshot()
        assert None in snap
        s = snap[None]
        assert s.attempts == 2
        assert s.successes == 1
        assert s.failures == 1
        assert s.latency_ms_count == 2
        assert s.retries_total == 1
        assert s.avg_latency_ms == 150.0
        assert s.success_rate == 0.5

    def test_per_tenant_separation(self) -> None:
        m = MetricsAccumulator()
        m.record(tenant_id="tenant-a", success=True, duration_ms=100.0)
        m.record(tenant_id="tenant-b", success=False, duration_ms=300.0)
        snap = m.snapshot()
        assert snap["tenant-a"].successes == 1
        assert snap["tenant-b"].failures == 1

    def test_negative_duration_rejects(self) -> None:
        m = MetricsAccumulator()
        with pytest.raises(ValueError, match="duration_ms"):
            m.record(tenant_id=None, success=True, duration_ms=-1.0)

    def test_negative_retries_rejects(self) -> None:
        m = MetricsAccumulator()
        with pytest.raises(ValueError, match="retries"):
            m.record(tenant_id=None, success=True, duration_ms=1.0, retries=-1)

    def test_snapshot_is_read_only_mapping(self) -> None:
        m = MetricsAccumulator()
        m.record(tenant_id=None, success=True, duration_ms=1.0)
        snap = m.snapshot()
        # MappingProxyType — no mutation allowed
        with pytest.raises(TypeError):
            snap[None] = MetricsSnapshot()  # type: ignore[index]
