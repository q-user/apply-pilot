"""`apply_once` orchestrator — JSON POST, status mapping, retry policy with backoff + jitter."""
from __future__ import annotations

import asyncio
import json
import logging
import random
from dataclasses import dataclass
from typing import Any

import httpx

from .client import HHApplyClient, NEGOTIATIONS_PATH, DEFAULT_BASE_URL
from .models import ApplyError, ApplyRequest, ApplyResult, ApplyStatus, HHApplyError

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RetryPolicy:
    """Retry / backoff parameters — see docs/integrations/hh_apply.md §5.

    Defaults match discovery doc. T3 (HHApplySettings) overrides at runtime.
    """

    max_retries: int = 3
    request_delay_ms: int = 750
    backoff_multiplier: float = 2.0
    jitter_ms: int = 200


def _negotiations_url() -> str:
    return DEFAULT_BASE_URL + NEGOTIATIONS_PATH


def _parse_response_body(response: httpx.Response) -> dict[str, Any]:
    try:
        raw = response.json()
        if isinstance(raw, dict):
            return raw
        return {"value": raw}
    except Exception:
        return {"text": response.text}


def _build_apply_result(
    status: ApplyStatus,
    http_status: int,
    raw: dict[str, Any],
    attempt_count: int,
    error: ApplyError | None = None,
) -> ApplyResult:
    negotiation_id: str | None = None
    if isinstance(raw, dict):
        nid = raw.get("id") or raw.get("negotiation_id")
        if isinstance(nid, str):
            negotiation_id = nid
    return ApplyResult(
        status=status,
        negotiation_id=negotiation_id,
        http_status=http_status,
        raw=raw,
        attempt_count=attempt_count,
        error=error,
    )


async def apply_once(
    request: ApplyRequest,
    *,
    client: HHApplyClient,
    retry_policy: RetryPolicy | None = None,
) -> ApplyResult:
    """Submit a single hh.ru apply via the Android-emulated POST `/negotiations`.

    Maps HTTP status codes to `ApplyStatus` per docs/integrations/hh_apply.md §1.
    Retries on 429 + 5xx per `RetryPolicy`. Returns `ApplyResult` on terminal state.
    Raises `HHApplyError` only on unrecoverable conditions (invalid request shape,
    session dead after refresh, etc.) — callers in `apply_worker` should log + count
    and proceed to the next vacancy rather than crash the worker.
    """
    if not request.message:
        raise HHApplyError("ApplyRequest.message must be non-empty (HH rejects empties with 400)")

    policy = retry_policy or RetryPolicy()
    delay_ms = policy.request_delay_ms
    last_retry_status: int | None = None
    last_retry_raw: dict[str, Any] | None = None
    url = _negotiations_url()
    payload = {
        "vacancy_id": request.vacancy_id,
        "resume_id": request.resume_id,
        "message": request.message,
        "lux": request.lux,
        "force": request.force,
    }

    for attempt in range(1, policy.max_retries + 1):
        logger.debug(
            "hh_apply: attempt %d/%d for vacancy %s",
            attempt, policy.max_retries, request.vacancy_id,
        )
        try:
            response = await client.request_with_xsrf_retry(
                "POST",
                url,
                content=json.dumps(payload),
                headers={"Content-Type": "application/json"},
            )
        except HHApplyError:
            raise
        except Exception as exc:
            last_retry_status = 0
            last_retry_raw = {"exception": type(exc).__name__, "message": str(exc)}
            logger.warning(
                "hh_apply: transport exception on attempt %d: %s",
                attempt, exc,
            )
            jitter = random.uniform(0, policy.jitter_ms)
            await asyncio.sleep((delay_ms + jitter) / 1000.0)
            delay_ms = int(delay_ms * policy.backoff_multiplier)
            continue

        status_code = response.status_code
        raw = _parse_response_body(response)

        if status_code in (200, 201):
            return _build_apply_result(
                ApplyStatus.success, status_code, raw, attempt,
            )
        if status_code == 400:
            err = ApplyError(
                code="validation_error", message="Bad request payload from server",
                http_status=400, raw=raw,
            )
            return _build_apply_result(
                ApplyStatus.validation_error, status_code, raw, attempt, error=err,
            )
        if status_code == 401:
            # client.request_with_xsrf_retry already attempted one refresh; terminal here.
            err = ApplyError(
                code="csrf_invalid",
                message="CSRF/XSRF invalid after refresh — session dead",
                http_status=401, raw=raw,
            )
            return _build_apply_result(
                ApplyStatus.auth_required, status_code, raw, attempt, error=err,
            )
        if status_code == 409:
            return _build_apply_result(
                ApplyStatus.idle_already_applied, status_code, raw, attempt,
            )
        if status_code == 429:
            last_retry_status, last_retry_raw = 429, raw
            logger.warning(
                "hh_apply: 429 rate-limited (attempt %d/%d) — backing off",
                attempt, policy.max_retries,
            )
        elif status_code >= 500:
            last_retry_status, last_retry_raw = status_code, raw
            logger.warning(
                "hh_apply: %d upstream error (attempt %d/%d) — backing off",
                status_code, attempt, policy.max_retries,
            )
        else:
            raise HHApplyError(
                f"hh_apply: unexpected HTTP status {status_code} (not in contract doc §1)"
            )

        # Retryable status: backoff then loop.
        jitter = random.uniform(0, policy.jitter_ms)
        await asyncio.sleep((delay_ms + jitter) / 1000.0)
        delay_ms = int(delay_ms * policy.backoff_multiplier)

    # Exhausted retries — return rate-limited or upstream-error terminal.
    if last_retry_status == 429:
        err = ApplyError(
            code="rate_limited", message="Too many requests after retries",
            http_status=429, raw=last_retry_raw,
        )
        return _build_apply_result(
            ApplyStatus.rate_limited, 429, last_retry_raw or {}, policy.max_retries, error=err,
        )
    err = ApplyError(
        code="upstream_error",
        message=f"Upstream {last_retry_status or 0} after retries",
        http_status=last_retry_status or 0, raw=last_retry_raw,
    )
    return _build_apply_result(
        ApplyStatus.upstream_error,
        last_retry_status or 0,
        last_retry_raw or {},
        policy.max_retries,
        error=err,
    )
