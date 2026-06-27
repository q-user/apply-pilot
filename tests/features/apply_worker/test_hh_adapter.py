"""Tests for HHApplyAdapter: cover-letter renderer must be invoked exactly once per submit (issue #294)."""

from __future__ import annotations

import asyncio
import uuid

import pytest

from apply_pilot.features.apply_worker.hh_adapter import HHApplyAdapter
from apply_pilot.features.apply_worker.models import ApplyJob
from apply_pilot.features.apply_worker.repository import InMemoryIdempotencyTracker
from apply_pilot.features.hh_apply import ApplyRequest, ApplyStatus, HHApplyResult


class _CountingRenderer:
    """Renderer that increments a counter and returns a deterministic body."""

    def __init__(self) -> None:
        self.calls = 0

    def __call__(self, job: ApplyJob) -> str:
        self.calls += 1
        return "hello-cover-letter"


def _job() -> ApplyJob:
    return ApplyJob(
        id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        vacancy_id=uuid.uuid4(),
        resume_id="resume-1",
        status="queued",
        payload={},
    )


@pytest.mark.asyncio
async def test_submit_invokes_cover_letter_renderer_once(monkeypatch: pytest.MonkeyPatch) -> None:
    """Regression for issue #294.

    The previous implementation called ``_cover_letter_renderer`` twice
    per submit — once via ``_build_idempotency_key`` and once for the
    ``ApplyRequest``. Renderers may hit the LLM, so the second call
    doubled latency for every apply attempt.
    """
    renderer = _CountingRenderer()
    adapter = HHApplyAdapter(
        idempotency_tracker=InMemoryIdempotencyTracker(),
        cover_letter_renderer=renderer,
    )

    # Stub ``apply_once`` so we never make a real HTTP call.
    async def _fake_apply_once(request: ApplyRequest, *, client, retry_policy) -> HHApplyResult:
        return HHApplyResult(
            status=ApplyStatus.success,
            attempt_count=1,
            http_status=200,
            negotiation_id="n-1",
            error=None,
        )

    monkeypatch.setattr("apply_pilot.features.apply_worker.hh_adapter.apply_once", _fake_apply_once)

    await adapter.submit(_job())

    assert renderer.calls == 1, (
        f"cover_letter_renderer must be called exactly once per submit, got {renderer.calls}"
    )


@pytest.mark.asyncio
async def test_submit_skips_request_when_idempotent(monkeypatch: pytest.MonkeyPatch) -> None:
    """Renderer counts: even on idempotent replay the renderer is called once."""
    renderer = _CountingRenderer()
    tracker = InMemoryIdempotencyTracker()
    adapter = HHApplyAdapter(
        idempotency_tracker=tracker,
        cover_letter_renderer=renderer,
    )
    job = _job()
    key = adapter._build_idempotency_key(
        job,
        adapter.tenant_provider.resolve(getattr(job, "tenant_id", None)),
        cover_letter="hello-cover-letter",
    )
    await tracker.record_success(key, "n-idempotent")

    await adapter.submit(job)

    # Replay path still pays one render (used in _build_idempotency_key).
    assert renderer.calls == 1
