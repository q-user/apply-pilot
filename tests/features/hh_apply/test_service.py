"""apply_once — status mapping, retry policy, payload shape (force/lux/message)."""
from __future__ import annotations

import json

import httpx
import pytest

from apply_pilot.features.hh_apply.client import HHApplyClient
from apply_pilot.features.hh_apply.models import (
    ApplyRequest,
    ApplyResult,
    ApplyStatus,
)
from apply_pilot.features.hh_apply.service import apply_once, RetryPolicy


def _client_with_responses(responses: list[httpx.Response]) -> HHApplyClient:
    """httpx.MockTransport that returns the next queued response on each call."""
    queue = list(responses)

    def handler(request: httpx.Request) -> httpx.Response:
        if not queue:
            return httpx.Response(599, request=request, text="test run out of queued responses")
        return queue.pop(0)

    transport = httpx.MockTransport(handler)
    return HHApplyClient(transport=transport)


def _post_json(request: httpx.Request) -> dict:
    return json.loads(request.content.decode("utf-8"))


class TestApplyOnceStatusMapping:
    @pytest.mark.asyncio
    async def test_201_success(self) -> None:
        client = _client_with_responses([
            httpx.Response(201, json={"id": "neg-1", "vacancy_id": "v1", "state": "applied"}),
        ])
        result = await apply_once(
            ApplyRequest(vacancy_id="v1", resume_id="r1", message="hi"),
            client=client,
        )
        assert result.status == ApplyStatus.success
        assert result.negotiation_id == "neg-1"
        assert result.http_status == 201
        assert result.attempt_count == 1

    @pytest.mark.asyncio
    async def test_400_validation_error(self) -> None:
        client = _client_with_responses([
            httpx.Response(400, json={"errors": {"message": ["Required field"]}}),
        ])
        result = await apply_once(
            ApplyRequest(vacancy_id="v1", resume_id="r1", message="hi"),
            client=client,
        )
        assert result.status == ApplyStatus.validation_error
        assert result.error is not None
        assert result.error.code == "validation_error"

    @pytest.mark.asyncio
    async def test_409_idle_already_applied(self) -> None:
        client = _client_with_responses([
            httpx.Response(409, json={"error": "already_applied", "negotiation_id": "neg-2"}),
        ])
        result = await apply_once(
            ApplyRequest(vacancy_id="v1", resume_id="r1", message="hi"),
            client=client,
        )
        assert result.status == ApplyStatus.idle_already_applied
        assert result.attempt_count == 1

    @pytest.mark.asyncio
    async def test_429_rate_limited_after_retry_exhaustion(self) -> None:
        # Configure tight backoff so retries do not stall the test
        rapid = RetryPolicy(request_delay_ms=0, jitter_ms=0)
        client = _client_with_responses([
            httpx.Response(429, text="throttled"),
            httpx.Response(429, text="throttled"),
            httpx.Response(429, text="throttled"),
        ])
        result = await apply_once(
            ApplyRequest(vacancy_id="v1", resume_id="r1", message="hi"),
            client=client, retry_policy=rapid,
        )
        assert result.status == ApplyStatus.rate_limited
        assert result.attempt_count == 3

    @pytest.mark.asyncio
    async def test_5xx_upstream_error_after_retry_exhaustion(self) -> None:
        rapid = RetryPolicy(request_delay_ms=0, jitter_ms=0)
        client = _client_with_responses([
            httpx.Response(500, text="server kaboom"),
            httpx.Response(503, text="server kaboom"),
            httpx.Response(500, text="server kaboom"),
        ])
        result = await apply_once(
            ApplyRequest(vacancy_id="v1", resume_id="r1", message="hi"),
            client=client, retry_policy=rapid,
        )
        assert result.status == ApplyStatus.upstream_error
        assert result.attempt_count == 3


class TestApplyOncePayload:
    @pytest.mark.asyncio
    async def test_payload_contains_vacancy_resume_message_lux_force(self) -> None:
        captured: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            captured.append(request)
            return httpx.Response(201, json={"id": "neg-1"})

        transport = httpx.MockTransport(handler)
        client = HHApplyClient(transport=transport)
        await apply_once(
            ApplyRequest(
                vacancy_id="v1", resume_id="r1", message="hello",
                lux=True, force=True,
            ),
            client=client,
        )
        assert len(captured) == 1
        payload = _post_json(captured[0])
        assert payload["vacancy_id"] == "v1"
        assert payload["resume_id"] == "r1"
        assert payload["message"] == "hello"
        assert payload["lux"] is True
        assert payload["force"] is True
        # JSON content-type header per docs §4.1
        assert captured[0].headers.get("Content-Type") == "application/json"


class TestApplyOnceContentType:
    @pytest.mark.asyncio
    async def test_uses_json_not_form_urlencoded(self) -> None:
        captured: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            captured.append(request)
            return httpx.Response(201, json={"id": "neg-x"})

        transport = httpx.MockTransport(handler)
        client = HHApplyClient(transport=transport)
        await apply_once(
            ApplyRequest(vacancy_id="v1", resume_id="r1", message="hi"),
            client=client,
        )
        # Crucial: must be application/json (per docs §4.1), not urlencoded.
        ct = captured[0].headers.get("Content-Type", "")
        assert ct.startswith("application/json")
