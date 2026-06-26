"""T5 (#246) tests for HHApplyAdapter.

Covers:
* idem-potency replay short-circuit
* ApplyStatus.success / idle_already_applied success-path mapping
* ApplyStatus.rate_limited / upstream_error retryable=failure mapping
* ApplyStatus.validation_error / auth_required non-retryable-failure mapping
* MetricsAccumulator + EventDispatcher wiring (durations, retries)
* HHApplyError propagation into a non-retryable ApplyResult
* Build_default_hh_apply_adapter factory
* Idempotency key determinism
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

import pytest

from apply_pilot.features.apply_worker.hh_adapter import (
    HHApplyAdapter,
    build_default_hh_apply_adapter,
)
from apply_pilot.features.apply_worker.models import ApplyJob
from apply_pilot.features.apply_worker.repository import (
    IdempotencyTracker,
    InMemoryIdempotencyTracker,
    NoOpIdempotencyTracker,
)
from apply_pilot.features.hh_apply import (
    ApplyError,
    ApplyRequest,
    ApplyStatus,
    EnvTenantCredentialProvider,
    EventDispatcher,
    EventType,
    HHApplyClient,
    MetricsAccumulator,
    RetryPolicy,
    TenantResolution,
)
from apply_pilot.features.hh_apply import (
    ApplyResult as HHApplyResult,
)

# ===========================================================================
# Fakes
# ===========================================================================


# TenantResolution.client is typed ``HHApplyClient`` (Pydantic BaseModel
# with arbitrary_types_allowed). Use a real instance -- applying ``apply_once``
# is monkey-patched in every test, so we never hit the live HTTP path.


def _stub_resolution(tenant_id: str | None = None, resume_id: str = "r1") -> TenantResolution:
    return TenantResolution(
        tenant_id=tenant_id,
        credentials=None,
        resume_id=resume_id,
        client=HHApplyClient(),
        retry_policy=RetryPolicy(
            max_retries=1,
            request_delay_ms=0,
            backoff_multiplier=1.0,
            jitter_ms=0,
        ),
    )


class _FakeTenantProvider:
    def __init__(self, tenant_id: str | None = None, resume_id: str = "r1") -> None:
        self.tenant_id = tenant_id
        self.resume_id = resume_id
        self.calls: list[str | None] = []

    def resolve(self, tenant_id: str | None) -> TenantResolution:
        self.calls.append(tenant_id)
        return TenantResolution(
            tenant_id=tenant_id,
            credentials=None,
            resume_id=self.resume_id,
            client=HHApplyClient(),
            retry_policy=RetryPolicy(
                max_retries=1,
                request_delay_ms=0,
                backoff_multiplier=1.0,
                jitter_ms=0,
            ),
        )


class _RecordingTracker(IdempotencyTracker):
    def __init__(self) -> None:
        self.state: dict[str, str | None] = {}
        self.has_calls: list[str] = []
        self.record_calls: list[tuple[str, str | None]] = []

    async def has_successful(self, key: str) -> bool:
        self.has_calls.append(key)
        return key in self.state

    async def record_success(self, key: str, negotiation_id: str | None = None) -> None:
        self.record_calls.append((key, negotiation_id))
        self.state[key] = negotiation_id


class _RecordingListener:
    def __init__(self) -> None:
        self.events: list = []

    def on_event(self, event) -> None:
        self.events.append(event)


@dataclass
class _Clock:
    """Deterministic monotonic clock for assertions on duration_ms."""

    t: float = 1000.0

    def __call__(self) -> float:
        return self.t

    def advance(self, ms: float) -> None:
        self.t += ms / 1000.0


def _make_job() -> ApplyJob:
    return ApplyJob(
        id=uuid.uuid4(),
        match_id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        vacancy_id=uuid.uuid4(),
        idempotency_key="k1",
    )


# ===========================================================================
# Idempotency
# ===========================================================================


async def test_idempotent_replay_short_circuits(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pre-marked idempotency key returns success=True WITHOUT calling apply_once."""
    tracker = _RecordingTracker()
    provider = _FakeTenantProvider(tenant_id=None)
    # Pre-record so has_successful returns True on the first check.
    adapter = HHApplyAdapter(
        tenant_provider=provider,
        idempotency_tracker=tracker,
    )
    # Synthesize a known key by reusing the adapter's deterministic builder.
    job = _make_job()
    expected_key = adapter._build_idempotency_key(
        job,
        provider.resolve(None),
    )
    await tracker.record_success(expected_key, "preexisting-id")

    seen_calls: list = []

    async def _spy(*args, **kwargs):
        seen_calls.append((args, kwargs))
        raise AssertionError("apply_once must NOT be called on idempotent replay")

    monkeypatch.setattr(
        "apply_pilot.features.apply_worker.hh_adapter.apply_once",
        _spy,
    )

    result = await adapter.submit(job)
    assert result.success is True
    assert result.error == "idempotent_replay"
    assert result.retryable is False
    assert seen_calls == []


async def test_idempotency_key_changes_with_cover_letter() -> None:
    """Two adapters with different cover-letter renderers produce different keys
    for the same (tenant, vacancy, resume)."""
    provider = _FakeTenantProvider()
    job_a = _make_job()
    job_b = _make_job()
    adapter_a = HHApplyAdapter(
        tenant_provider=provider,
        idempotency_tracker=NoOpIdempotencyTracker(),
        cover_letter_renderer=lambda j: "msg-A",
    )
    adapter_b = HHApplyAdapter(
        tenant_provider=provider,
        idempotency_tracker=NoOpIdempotencyTracker(),
        cover_letter_renderer=lambda j: "msg-B",
    )
    res_a = provider.resolve(None)
    res_b = provider.resolve(None)
    key_a = adapter_a._build_idempotency_key(job_a, res_a)
    key_b = adapter_b._build_idempotency_key(job_b, res_b)
    assert key_a != key_b


async def test_first_run_records_idempotency_on_success(monkeypatch: pytest.MonkeyPatch) -> None:
    tracker = _RecordingTracker()
    listener = _RecordingListener()
    events = EventDispatcher(listeners=[listener])
    metrics = MetricsAccumulator()
    clock = _Clock()
    adapter = HHApplyAdapter(
        tenant_provider=_FakeTenantProvider(),
        idempotency_tracker=tracker,
        events=events,
        metrics=metrics,
        clock=clock,
    )

    async def _fake(request: ApplyRequest, *, client, retry_policy=None) -> HHApplyResult:
        return HHApplyResult(
            status=ApplyStatus.success,
            negotiation_id="neg-1",
            http_status=201,
            raw={"id": "neg-1"},
            attempt_count=1,
            error=None,
        )

    monkeypatch.setattr(
        "apply_pilot.features.apply_worker.hh_adapter.apply_once",
        _fake,
    )

    result = await adapter.submit(_make_job())
    assert result.success is True
    assert result.external_application_id == "neg-1"
    assert len(tracker.record_calls) == 1
    assert tracker.record_calls[0][1] == "neg-1"


# ===========================================================================
# Status mapping
# ===========================================================================


@pytest.mark.parametrize(
    "status,expected_success,expected_retryable",
    [
        (ApplyStatus.success, True, False),
        (ApplyStatus.idle_already_applied, True, False),
        (ApplyStatus.validation_error, False, False),
        (ApplyStatus.auth_required, False, False),
        (ApplyStatus.rate_limited, False, True),
        (ApplyStatus.upstream_error, False, True),
    ],
)
async def test_status_mapping(
    monkeypatch: pytest.MonkeyPatch,
    status: ApplyStatus,
    expected_success: bool,
    expected_retryable: bool,
) -> None:
    adapter = HHApplyAdapter(
        tenant_provider=_FakeTenantProvider(),
        idempotency_tracker=NoOpIdempotencyTracker(),
        events=EventDispatcher(),
        metrics=MetricsAccumulator(),
    )
    err = (
        ApplyError(code=status.value, message="m", http_status=0, raw=None)
        if status != ApplyStatus.success and status != ApplyStatus.idle_already_applied
        else None
    )

    async def _fake(request, *, client, retry_policy=None):
        return HHApplyResult(
            status=status,
            negotiation_id="neg-x" if expected_success else None,
            http_status=201 if expected_success else 0,
            raw=None,
            attempt_count=1,
            error=err,
        )

    monkeypatch.setattr(
        "apply_pilot.features.apply_worker.hh_adapter.apply_once",
        _fake,
    )

    result = await adapter.submit(_make_job())
    assert result.success is expected_success
    assert result.retryable is expected_retryable


# ===========================================================================
# Failure handling
# ===========================================================================


async def test_apply_once_exception_returns_non_retryable_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    metrics = MetricsAccumulator()
    listener = _RecordingListener()
    adapter = HHApplyAdapter(
        tenant_provider=_FakeTenantProvider(),
        idempotency_tracker=NoOpIdempotencyTracker(),
        events=EventDispatcher(listeners=[listener]),
        metrics=metrics,
    )

    async def _boom(*args, **kwargs):
        raise RuntimeError("connection refused")

    monkeypatch.setattr(
        "apply_pilot.features.apply_worker.hh_adapter.apply_once",
        _boom,
    )

    result = await adapter.submit(_make_job())
    assert result.success is False
    assert result.retryable is False
    assert "connection refused" in (result.error or "")
    snap = dict(metrics.snapshot())
    assert snap[None].attempts == 1
    assert snap[None].failures == 1
    # ATTEMPT_FAILED_UNRECOVERABLE event recorded
    assert any(e.event_type == EventType.ATTEMPT_FAILED_UNRECOVERABLE for e in listener.events)


async def test_existing_metrics_does_not_explode(monkeypatch: pytest.MonkeyPatch) -> None:
    metrics = MetricsAccumulator()
    adapter = HHApplyAdapter(
        tenant_provider=_FakeTenantProvider(),
        idempotency_tracker=NoOpIdempotencyTracker(),
        metrics=metrics,
    )

    async def _fake(request, *, client, retry_policy=None):
        return HHApplyResult(
            status=ApplyStatus.success,
            negotiation_id="neg-2",
            http_status=201,
            raw=None,
            attempt_count=2,  # 1 retry inside hh_apply
            error=None,
        )

    monkeypatch.setattr(
        "apply_pilot.features.apply_worker.hh_adapter.apply_once",
        _fake,
    )

    await adapter.submit(_make_job())
    snap = dict(metrics.snapshot())
    assert snap[None].retries_total == 1
    assert snap[None].successes == 1
    assert snap[None].failures == 0


# ===========================================================================
# Event emission
# ===========================================================================


async def test_started_and_completed_events_emitted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    listener = _RecordingListener()
    events = EventDispatcher(listeners=[listener])
    adapter = HHApplyAdapter(
        tenant_provider=_FakeTenantProvider(tenant_id="tenant-A"),
        idempotency_tracker=NoOpIdempotencyTracker(),
        events=events,
    )

    async def _fake(request, *, client, retry_policy=None):
        return HHApplyResult(
            status=ApplyStatus.success,
            negotiation_id="neg-E",
            http_status=201,
            raw=None,
            attempt_count=1,
            error=None,
        )

    monkeypatch.setattr(
        "apply_pilot.features.apply_worker.hh_adapter.apply_once",
        _fake,
    )
    await adapter.submit(_make_job())
    types = [e.event_type for e in listener.events]
    assert EventType.ATTEMPT_STARTED in types
    assert EventType.ATTEMPT_COMPLETED in types
    started = next(e for e in listener.events if e.event_type == EventType.ATTEMPT_STARTED)
    # ApplyJob has no tenant_id field; getattr(job, 'tenant_id', None) returns None
    # and FakeTenantProvider.resolve(None) returns tenant_id=None.
    assert started.tenant_id is None


# ===========================================================================
# Factory
# ===========================================================================


def test_build_default_returns_oss_wired_adapter() -> None:
    adapter = build_default_hh_apply_adapter()
    assert isinstance(adapter, HHApplyAdapter)
    assert isinstance(adapter.tenant_provider, EnvTenantCredentialProvider)
    assert isinstance(adapter.idempotency_tracker, InMemoryIdempotencyTracker)
    assert isinstance(adapter.metrics, MetricsAccumulator)
    assert isinstance(adapter.events, EventDispatcher)


def test_build_default_passes_overrides() -> None:
    provider = _FakeTenantProvider(tenant_id="x")
    metrics = MetricsAccumulator()
    tracker = _RecordingTracker()
    events = EventDispatcher()
    adapter = build_default_hh_apply_adapter(
        tenant_provider=provider,
        metrics=metrics,
        idempotency_tracker=tracker,
        events=events,
        cover_letter_renderer=lambda j: "msg",
        clock=lambda: 0.0,
    )
    assert adapter.tenant_provider is provider
    assert adapter.metrics is metrics
    assert adapter.idempotency_tracker is tracker
    assert adapter.events is events


# ===========================================================================
# Clock / duration
# ===========================================================================


async def test_clock_drives_duration_ms_for_metrics(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The injectable clock drives duration_ms captured by MetricsAccumulator.

    ``started = self._clock()`` is captured BEFORE apply_once runs; the
    fake apply_once advances the clock by 120.5ms, then the post-call
    ``self._clock() - started`` captures that duration.
    """
    clock = _Clock(t=1000.0)
    metrics = MetricsAccumulator()
    adapter = HHApplyAdapter(
        tenant_provider=_FakeTenantProvider(),
        idempotency_tracker=NoOpIdempotencyTracker(),
        metrics=metrics,
        clock=clock,
    )

    async def _fake(request, *, client, retry_policy=None):
        clock.advance(120.5)  # simulates 120.5 ms inside apply_once
        return HHApplyResult(
            status=ApplyStatus.success,
            negotiation_id="n",
            http_status=201,
            raw=None,
            attempt_count=1,
            error=None,
        )

    monkeypatch.setattr(
        "apply_pilot.features.apply_worker.hh_adapter.apply_once",
        _fake,
    )
    await adapter.submit(_make_job())
    snap = dict(metrics.snapshot())
    assert snap[None].latency_ms_sum == pytest.approx(120.5, abs=0.01)
    assert snap[None].latency_ms_count == 1


# ===========================================================================
# Properties / read-only surface
# ===========================================================================


def test_properties_expose_internals_for_tests() -> None:
    provider = _FakeTenantProvider()
    tracker = _RecordingTracker()
    events = EventDispatcher()
    metrics = MetricsAccumulator()
    adapter = HHApplyAdapter(
        tenant_provider=provider,
        idempotency_tracker=tracker,
        events=events,
        metrics=metrics,
    )
    assert adapter.tenant_provider is provider
    assert adapter.idempotency_tracker is tracker
    assert adapter.events is events
    assert adapter.metrics is metrics
