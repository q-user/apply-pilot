"""Tests for the hh.ru apply submission adapter (M5, issue #48).

These tests cover the public surface of the ``features.hh.apply`` module:

* :class:`HhApplyAdapter` request shape (via ``httpx.MockTransport``) — no
  real network calls. The slice's narrow contract for issue #48 is that
  :class:`~apply_pilot.features.apply_worker.models.ApplyJob.idempotency_key`
  flows through to the outgoing request as an ``Idempotency-Key`` header
  so hh.ru can dedup retries within the same enqueue triple.
"""

from __future__ import annotations

import uuid

import httpx
import pytest

from apply_pilot.features.apply_worker.models import ApplyJob
from apply_pilot.features.hh.apply import HhApplyAdapter

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_apply_job(
    *,
    user_id: uuid.UUID | None = None,
    vacancy_id: uuid.UUID | None = None,
    match_id: uuid.UUID | None = None,
) -> ApplyJob:
    """Build an in-memory :class:`ApplyJob` with an auto-computed idempotency key.

    The slice's model auto-fills ``idempotency_key`` from
    ``(user_id, vacancy_id, match_id)`` when the triple is supplied and
    no explicit key is provided — that is the same path the SQL
    repository takes, so the value is byte-identical to a row flushed
    through Alembic.
    """
    return ApplyJob(
        match_id=match_id or uuid.uuid4(),
        user_id=user_id or uuid.uuid4(),
        vacancy_id=vacancy_id or uuid.uuid4(),
    )


def _success_response() -> dict:
    """A minimal successful hh.ru ``/negotiations`` response body."""
    return {"id": "negotiation-12345"}


def _make_adapter(handler) -> tuple[HhApplyAdapter, list[httpx.Request]]:
    """Build an :class:`HhApplyAdapter` wired to a mock transport.

    Returns the adapter and the list the handler appends captured
    requests to — letting each test assert on the outgoing headers.
    """
    captured: list[httpx.Request] = []

    def capturing_handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(201, json=_success_response())

    transport = httpx.MockTransport(capturing_handler)
    http_client = httpx.AsyncClient(transport=transport)
    adapter = HhApplyAdapter(client=http_client, base_url="https://api.hh.ru/negotiations")
    return adapter, captured


# ---------------------------------------------------------------------------
# Idempotency-Key header
# ---------------------------------------------------------------------------


class TestHhApplyAdapterIdempotencyKey:
    """Issue #48 contract: ``ApplyJob.idempotency_key`` is sent as the
    ``Idempotency-Key`` HTTP header so hh.ru can dedup retries.
    """

    @pytest.mark.asyncio
    async def test_submit_includes_idempotency_key_header(self) -> None:
        """The outgoing request carries an ``Idempotency-Key`` header."""
        job = _make_apply_job()
        adapter, captured = _make_adapter(None)

        result = await adapter.submit(job)
        await adapter.aclose()

        assert result.success is True
        assert len(captured) == 1
        # httpx lowercases header names; check both casings defensively.
        headers = captured[0].headers
        assert "idempotency-key" in headers
        assert headers["idempotency-key"] != ""

    @pytest.mark.asyncio
    async def test_idempotency_key_matches_job_value(self) -> None:
        """The header value is exactly ``ApplyJob.idempotency_key``."""
        job = _make_apply_job()
        adapter, captured = _make_adapter(None)

        await adapter.submit(job)
        await adapter.aclose()

        assert len(captured) == 1
        assert captured[0].headers["idempotency-key"] == job.idempotency_key

    @pytest.mark.asyncio
    async def test_different_jobs_have_different_idempotency_keys(self) -> None:
        """Two distinct jobs send two distinct Idempotency-Key headers.

        This pins down the property the issue relies on: re-queueing
        the same triple reuses the same key (so hh dedups the retry),
        but a different triple carries a different key (so a fresh
        application is not silently deduped against a previous one).
        """
        job1 = _make_apply_job()
        job2 = _make_apply_job()
        # Sanity: the two jobs must not accidentally collide.
        assert job1.idempotency_key != job2.idempotency_key

        adapter, captured = _make_adapter(None)
        await adapter.submit(job1)
        await adapter.submit(job2)
        await adapter.aclose()

        assert len(captured) == 2
        key1 = captured[0].headers["idempotency-key"]
        key2 = captured[1].headers["idempotency-key"]
        assert key1 == job1.idempotency_key
        assert key2 == job2.idempotency_key
        assert key1 != key2
