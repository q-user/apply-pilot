"""T5 (#246) worker-integration wiring: ``apply_worker`` -> ``hh_apply.apply_once``.

Implements the existing :class:`ApplyAdapter` Protocol seam so production
runtime plugs it into:

    ApplyWorker(adapters={"hh": HHApplyAdapter(...)}, ...)

without rippling any constructor changes through :class:`ApplyWorker`
or :class:`ApplyJobService`. Existing apply_worker tests stay green.

Source-of-truth contract
------------------------

* ``docs/integrations/hh_apply.md`` (M11 T1, #242, merged at fbed762).
* Payload shape: ``{"vacancy_id","resume_id","message","lux","force"}`` with
  ``Content-Type: application/json`` (NOT urlencoded, per doc section 4.1).
* Status mapping in :meth:`HHApplyAdapter.submit` follows
  ``docs/integrations/hh_apply.md`` section 1.

M11 deprecation sentinel (from #239):

* No ``selenium`` / ``playwright`` / ``pywebview`` / captcha solvers.
* No vendor tree under ``/home/mikhail/projects/hh_apply`` --
  imports stay inside ``apply_pilot.features.hh_apply``.
"""

from __future__ import annotations

import hashlib
import logging
import time
from collections.abc import Callable
from typing import Any

from apply_pilot.features.apply_worker.models import ApplyJob
from apply_pilot.features.apply_worker.repository import (
    IdempotencyTracker,
    InMemoryIdempotencyTracker,
)
from apply_pilot.features.apply_worker.runtime import ApplyResult
from apply_pilot.features.hh_apply import (
    ApplyEvent,
    ApplyRequest,
    ApplyStatus,
    EnvTenantCredentialProvider,
    EventDispatcher,
    EventType,
    HHApplySettings,
    MetricsAccumulator,
    TenantCredentialProvider,
    TenantResolution,
    apply_once,
)

logger = logging.getLogger(__name__)


_ADAPTER_NAME: str = "hh"


def _default_cover_letter(job: ApplyJob) -> str:
    """Fallback cover-letter renderer.

    Returns an empty string when the caller has not wired a real
    renderer. Per ``docs/integrations/hh_apply.md`` section 4.1 an
    empty ``message`` is rejected by HH with HTTP 400, so production
    must inject a real renderer; tests get away with the empty value
    because they monkey-patch :func:`apply_once` and never reach the
    real HTTP path.
    """
    return ""


class HHApplyAdapter:
    """Adapter that bridges an :class:`ApplyJob` to ``hh_apply.apply_once``.

    Lifecycle per submission:

    1. Resolve per-tenant credentials (and the ready-to-use
       :class:`HHApplyClient` + :class:`RetryPolicy` pair) via the
       injected :class:`TenantCredentialProvider`.
    2. Short-circuit on idempotent replay when
       :meth:`IdempotencyTracker.has_successful` returns ``True``.
    3. Build :class:`ApplyRequest` with vacancy_id / resume_id / message
       and ``force=False`` (worker-controlled retries use ``force=True``
       in the future; T5 keeps the contract at ``force=False``).
    4. Emit :attr:`EventType.ATTEMPT_STARTED`.
    5. ``await apply_once(...)`` inside a ``try/except`` for
       :class:`HHApplyError` plus any transport-level exception.
    6. Map ``hh_apply.ApplyResult`` + :class:`ApplyStatus` to
       :class:`apply_pilot.features.apply_worker.runtime.ApplyResult`.
    7. Emit ``ATTEMPT_COMPLETED`` / ``ATTEMPT_FAILED_RECOVERABLE`` /
       ``ATTEMPT_FAILED_UNRECOVERABLE`` per the mapping.
    8. Update :class:`MetricsAccumulator` with duration + retries.
    9. Stamp :meth:`IdempotencyTracker.record_success` on a successful
       (or idle_already_applied) response so a re-run of the same job
       short-circuits at step 2.
    """

    name: str = _ADAPTER_NAME

    def __init__(
        self,
        *,
        settings: HHApplySettings | None = None,
        tenant_provider: TenantCredentialProvider | None = None,
        idempotency_tracker: IdempotencyTracker | None = None,
        events: EventDispatcher | None = None,
        metrics: MetricsAccumulator | None = None,
        cover_letter_renderer: Callable[[ApplyJob], str] | None = None,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self._settings: HHApplySettings = settings or HHApplySettings()
        self._tenant_provider: TenantCredentialProvider = (
            tenant_provider or EnvTenantCredentialProvider(self._settings)
        )
        self._idempotency_tracker: IdempotencyTracker = (
            idempotency_tracker or InMemoryIdempotencyTracker()
        )
        self._events: EventDispatcher = events or EventDispatcher()
        self._metrics: MetricsAccumulator = metrics or MetricsAccumulator()
        self._cover_letter_renderer: Callable[[ApplyJob], str] = (
            cover_letter_renderer or _default_cover_letter
        )
        self._clock: Callable[[], float] = clock or time.monotonic

    @property
    def tenant_provider(self) -> TenantCredentialProvider:
        return self._tenant_provider

    @property
    def metrics(self) -> MetricsAccumulator:
        return self._metrics

    @property
    def events(self) -> EventDispatcher:
        return self._events

    @property
    def idempotency_tracker(self) -> IdempotencyTracker:
        return self._idempotency_tracker

    @property
    def settings(self) -> HHApplySettings:
        return self._settings

    def _build_idempotency_key(
        self,
        job: ApplyJob,
        resolution: TenantResolution,
    ) -> str:
        """Derive a stable idempotency key for ``(tenant, vacancy, resume, message)``.

        The cover-letter renderer is invoked TWICE -- once for the hash,
        once for the :class:`ApplyRequest`. Renderers MUST be
        deterministic for a given job (no timestamps, no randomness);
        a non-deterministic renderer breaks the replay guarantee.
        """
        h = hashlib.sha256()
        h.update((resolution.tenant_id or "").encode("utf-8"))
        h.update(b"|")
        h.update(str(job.vacancy_id).encode("utf-8"))
        h.update(b"|")
        h.update(resolution.resume_id.encode("utf-8"))
        h.update(b"|")
        h.update(self._cover_letter_renderer(job).encode("utf-8"))
        return f"hh:{h.hexdigest()[:16]}"

    async def submit(self, job: ApplyJob) -> ApplyResult:
        tenant_id: str | None = getattr(job, "tenant_id", None)
        resolution = self._tenant_provider.resolve(tenant_id)

        idempotency_key = self._build_idempotency_key(job, resolution)
        if await self._idempotency_tracker.has_successful(idempotency_key):
            logger.info(
                "apply_worker.hh_adapter: idempotent replay key=%s job=%s -- skipping",
                idempotency_key,
                job.id,
            )
            return ApplyResult(
                success=True,
                external_application_id=None,
                error="idempotent_replay",
                retryable=False,
            )

        cover_letter = self._cover_letter_renderer(job)
        request = ApplyRequest(
            vacancy_id=str(job.vacancy_id),
            resume_id=resolution.resume_id,
            message=cover_letter,
            lux=False,
            force=False,
        )

        self._events.emit(
            ApplyEvent(
                event_type=EventType.ATTEMPT_STARTED,
                tenant_id=resolution.tenant_id,
                vacancy_id=str(job.vacancy_id),
                resume_id=resolution.resume_id,
                attempt_count=1,
            )
        )

        started = self._clock()
        try:
            hh_result = await apply_once(
                request,
                client=resolution.client,
                retry_policy=resolution.retry_policy,
            )
        except Exception as exc:
            duration_ms = (self._clock() - started) * 1000.0
            self._metrics.record(
                tenant_id=resolution.tenant_id,
                success=False,
                duration_ms=duration_ms,
                retries=0,
            )
            self._events.emit(
                ApplyEvent(
                    event_type=EventType.ATTEMPT_FAILED_UNRECOVERABLE,
                    tenant_id=resolution.tenant_id,
                    vacancy_id=str(job.vacancy_id),
                    resume_id=resolution.resume_id,
                    attempt_count=1,
                    duration_ms=duration_ms,
                    error_code=type(exc).__name__,
                )
            )
            logger.warning(
                "apply_worker.hh_adapter: apply_once raised %s: %s",
                type(exc).__name__,
                exc,
            )
            return ApplyResult(
                success=False,
                external_application_id=None,
                error=f"hh_apply_exception: {exc}",
                retryable=False,
            )

        duration_ms = (self._clock() - started) * 1000.0
        retries = max(0, hh_result.attempt_count - 1)
        is_success = hh_result.status == ApplyStatus.success
        self._metrics.record(
            tenant_id=resolution.tenant_id,
            success=is_success,
            duration_ms=duration_ms,
            retries=retries,
        )

        if is_success or hh_result.status == ApplyStatus.idle_already_applied:
            await self._idempotency_tracker.record_success(
                idempotency_key, hh_result.negotiation_id
            )
            self._events.emit(
                ApplyEvent(
                    event_type=EventType.ATTEMPT_COMPLETED,
                    tenant_id=resolution.tenant_id,
                    vacancy_id=str(job.vacancy_id),
                    resume_id=resolution.resume_id,
                    status=hh_result.status,
                    http_status=hh_result.http_status,
                    attempt_count=hh_result.attempt_count,
                    duration_ms=duration_ms,
                    negotiation_id=hh_result.negotiation_id,
                )
            )
            return ApplyResult(
                success=True,
                external_application_id=hh_result.negotiation_id,
                error=None,
                retryable=False,
            )

        retryable = hh_result.status in (
            ApplyStatus.rate_limited,
            ApplyStatus.upstream_error,
        )
        event_type = (
            EventType.ATTEMPT_FAILED_RECOVERABLE
            if retryable
            else EventType.ATTEMPT_FAILED_UNRECOVERABLE
        )
        error_code = hh_result.error.code if hh_result.error is not None else hh_result.status.value
        self._events.emit(
            ApplyEvent(
                event_type=event_type,
                tenant_id=resolution.tenant_id,
                vacancy_id=str(job.vacancy_id),
                resume_id=resolution.resume_id,
                status=hh_result.status,
                http_status=hh_result.http_status,
                attempt_count=hh_result.attempt_count,
                duration_ms=duration_ms,
                error_code=error_code,
            )
        )
        return ApplyResult(
            success=False,
            external_application_id=None,
            error=f"hh_apply_status:{hh_result.status.value}",
            retryable=retryable,
        )


def build_default_hh_apply_adapter(**overrides: Any) -> HHApplyAdapter:
    settings = overrides.pop("settings", None) or HHApplySettings()
    tenant_provider = overrides.pop("tenant_provider", None) or EnvTenantCredentialProvider(
        settings
    )
    idempotency_tracker = overrides.pop("idempotency_tracker", None) or InMemoryIdempotencyTracker()
    events = overrides.pop("events", None) or EventDispatcher()
    metrics = overrides.pop("metrics", None) or MetricsAccumulator()
    cover_letter_renderer = overrides.pop("cover_letter_renderer", None)
    clock = overrides.pop("clock", None)
    return HHApplyAdapter(
        settings=settings,
        tenant_provider=tenant_provider,
        idempotency_tracker=idempotency_tracker,
        events=events,
        metrics=metrics,
        cover_letter_renderer=cover_letter_renderer,
        clock=clock,
        **overrides,
    )


__all__ = ["HHApplyAdapter", "build_default_hh_apply_adapter"]
