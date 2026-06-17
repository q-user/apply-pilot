"""Admin slice — integration health view (M6, issue #57).

This module owns the contract for reporting the health of every external
integration the service depends on (hh.ru OAuth, the LLM provider, the
database, ...). The slice has three moving parts:

* :class:`IntegrationStatus` — the immutable value object the
  ``GET /admin/integrations`` endpoint returns.
* :class:`InMemoryIntegrationStatusStore` — the in-process cache the
  worker writes to and the API reads from. The Protocol
  :class:`IntegrationStatusStore` keeps the storage pluggable.
* :class:`IntegrationStatusWorker` — a :class:`BaseProcess` subclass
  that periodically calls every :class:`IntegrationChecker` and writes
  the result into the store.

The slice is the single source of truth for ``admin/integrations``;
the FastAPI router in :mod:`job_apply.features.admin.api` is a thin
wrapper that maps the store to HTTP.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

import httpx

from job_apply.features.hh.oauth import HhHttpOAuthClient
from job_apply.features.scoring.llm import HttpLLMClient
from job_apply.runtime.process import BaseProcess

_LOG_PREFIX = "job_apply.features.admin.integrations."

#: Default sleep between :meth:`IntegrationStatusWorker.run` iterations.
#: 60 s keeps the API responsive without hammering external services.
DEFAULT_REFRESH_INTERVAL_SECONDS: float = 60.0

#: Stable status values the API contract promises. The set is small and
#: closed — the worker is the only producer and the API is the only
#: consumer, so adding a new value is a public-API change and should be
#: done deliberately.
STATUS_HEALTHY: str = "healthy"
STATUS_DEGRADED: str = "degraded"
STATUS_UNHEALTHY: str = "unhealthy"
STATUS_UNKNOWN: str = "unknown"

__all__ = [
    "DatabaseChecker",
    "DEFAULT_REFRESH_INTERVAL_SECONDS",
    "HhOAuthChecker",
    "InMemoryIntegrationStatusStore",
    "IntegrationChecker",
    "IntegrationStatus",
    "IntegrationStatusStore",
    "IntegrationStatusWorker",
    "LlmChecker",
    "STATUS_DEGRADED",
    "STATUS_HEALTHY",
    "STATUS_UNHEALTHY",
    "STATUS_UNKNOWN",
]


# ---------------------------------------------------------------------------
# Value object
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class IntegrationStatus:
    """Immutable snapshot of a single integration's health.

    Attributes
    ----------
    name:
        Stable identifier the API exposes (e.g. ``"hh"``, ``"llm"``,
        ``"database"``). The :class:`IntegrationStatusWorker` keys the
        store by this value.
    status:
        One of :data:`STATUS_HEALTHY`, :data:`STATUS_DEGRADED`,
        :data:`STATUS_UNHEALTHY`, or :data:`STATUS_UNKNOWN`. The set is
        the public contract; new values are a breaking change.
    last_checked_at:
        Wall-clock time (UTC) of the last :meth:`check` call. The
        worker refreshes this on every iteration; the API uses it to
        tell operators how stale the cached status is.
    error:
        Human-readable error message. ``None`` on success. The
        front-end may render this verbatim in the admin panel.
    metadata:
        Free-form structured context (latency, status code, ...).
        ``None`` when the check produced nothing useful.
    """

    name: str
    status: str
    last_checked_at: Any  # ``datetime`` with tzinfo; ``Any`` to avoid a runtime import
    error: str | None
    metadata: dict[str, Any] | None


# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------


@runtime_checkable
class IntegrationStatusStore(Protocol):
    """Persistence contract for integration health snapshots.

    The :class:`IntegrationStatusWorker` writes through :meth:`update`
    on every iteration; the API reads through :meth:`get_all` to render
    ``GET /admin/integrations``. The in-memory implementation
    (:class:`InMemoryIntegrationStatusStore`) is the default; tests can
    substitute a fake as long as it implements the same surface.
    """

    def get_all(self) -> list[IntegrationStatus]:
        """Return every known status, ordered by ``name`` for stability."""
        ...

    def update(self, name: str, status: IntegrationStatus) -> None:
        """Persist *status* under *name*, replacing any prior entry."""
        ...


class InMemoryIntegrationStatusStore:
    """Dict-backed :class:`IntegrationStatusStore` for dev and tests.

    The store keeps a single :class:`IntegrationStatus` per integration
    name. The class is intentionally not thread-safe — the worker and
    the API both run in the same asyncio event loop, and dict mutations
    happen between ``await`` points only.
    """

    __slots__ = ("_statuses",)

    def __init__(self) -> None:
        self._statuses: dict[str, IntegrationStatus] = {}

    def get_all(self) -> list[IntegrationStatus]:
        """Return every known status sorted by ``name`` for stable output."""
        return [self._statuses[name] for name in sorted(self._statuses)]

    def update(self, name: str, status: IntegrationStatus) -> None:
        """Insert or replace the status stored under *name*."""
        self._statuses[name] = status


# ---------------------------------------------------------------------------
# Checkers
# ---------------------------------------------------------------------------


@runtime_checkable
class IntegrationChecker(Protocol):
    """Contract every integration health check implements.

    The :class:`IntegrationStatusWorker` iterates over a list of
    concrete implementations and ``await``\s :meth:`check` on each.
    Implementations must be safe to call concurrently — the worker
    currently runs them sequentially, but a future improvement may
    fan out via :func:`asyncio.gather` to keep total refresh latency
    bounded by the slowest checker.
    """

    name: str

    async def check(self) -> IntegrationStatus:
        """Return the current health snapshot for this integration."""
        ...


def _now() -> Any:
    """Return the current UTC time. Wrapped so tests can monkey-patch it."""
    from datetime import UTC, datetime

    return datetime.now(UTC)


class HhOAuthChecker:
    """Health check for the hh.ru OAuth token endpoint.

    The check performs a ``refresh_tokens`` call with a synthetic
    refresh token. The endpoint is expected to reject the token —
    ``200`` and ``400`` (invalid_grant) both prove the service is
    reachable and speaking OAuth correctly. A ``401`` means the client
    credentials are wrong; any other failure mode is treated as
    ``unhealthy``.
    """

    name: str = "hh"

    def __init__(self, *, client: HhHttpOAuthClient) -> None:
        self._client = client
        self._logger = logging.getLogger(f"{_LOG_PREFIX}HhOAuthChecker")

    async def check(self) -> IntegrationStatus:
        """Run the check and translate the response into an :class:`IntegrationStatus`."""
        from job_apply.features.hh.oauth import OAuthExchangeError

        started = time.monotonic()
        # ``health-check`` is a synthetic refresh token — the endpoint is
        # expected to reject it. We are only testing reachability and
        # credential validity, not actually refreshing any user token.
        try:
            await self._client.refresh_tokens(refresh_token="health-check")
        except OAuthExchangeError as exc:
            latency_ms = int((time.monotonic() - started) * 1000)
            status_code = exc.status_code
            if status_code in (200, 400):
                # 200 means the endpoint ignored the bogus token and
                # still returned data (extreme edge case); 400 is the
                # common, healthy "invalid_grant" response.
                return IntegrationStatus(
                    name=self.name,
                    status=STATUS_HEALTHY,
                    last_checked_at=_now(),
                    error=None,
                    metadata={"status_code": status_code, "latency_ms": latency_ms},
                )
            # 401 means the configured client credentials are wrong.
            return IntegrationStatus(
                name=self.name,
                status=STATUS_UNHEALTHY,
                last_checked_at=_now(),
                error=f"hh.ru OAuth returned HTTP {status_code}: {exc}",
                metadata={"status_code": status_code, "latency_ms": latency_ms},
            )
        except Exception as exc:  # noqa: BLE001 — network/parse failures are unhealthy
            latency_ms = int((time.monotonic() - started) * 1000)
            self._logger.warning(
                "admin.integrations.hh_oauth_check_failed",
                extra={
                    "event": "admin.integrations.hh_oauth_check_failed",
                    "error": str(exc),
                },
            )
            return IntegrationStatus(
                name=self.name,
                status=STATUS_UNHEALTHY,
                last_checked_at=_now(),
                error=f"hh.ru OAuth check failed: {exc}",
                metadata={"latency_ms": latency_ms},
            )

        # 200 with a parsed body — the endpoint is healthy and the
        # credentials are valid. This branch is hit when hh.ru happens
        # to accept our synthetic token (it shouldn't, but treat it as
        # healthy because the whole point of the check is to be green).
        latency_ms = int((time.monotonic() - started) * 1000)
        return IntegrationStatus(
            name=self.name,
            status=STATUS_HEALTHY,
            last_checked_at=_now(),
            error=None,
            metadata={"status_code": 200, "latency_ms": latency_ms},
        )


class LlmChecker:
    """Health check for the OpenAI-compatible chat-completions endpoint.

    The check sends a tiny prompt and expects a 2xx response with a
    non-empty ``choices[0].message.content``. Any other outcome — a
    network error, a non-2xx status, a malformed body — is
    ``unhealthy``.
    """

    name: str = "llm"

    #: Prompt used by the health check. Kept short and cheap to score
    #: so the check stays cheap even on slow providers.
    _PROBE_PROMPT: str = "ping"

    def __init__(self, *, client: HttpLLMClient) -> None:
        self._client = client
        self._logger = logging.getLogger(f"{_LOG_PREFIX}LlmChecker")

    async def check(self) -> IntegrationStatus:
        started = time.monotonic()
        try:
            content = await self._client.complete(
                self._PROBE_PROMPT,
                temperature=0.0,
                max_tokens=8,
            )
        except httpx.HTTPStatusError as exc:
            latency_ms = int((time.monotonic() - started) * 1000)
            return IntegrationStatus(
                name=self.name,
                status=STATUS_UNHEALTHY,
                last_checked_at=_now(),
                error=f"LLM responded with HTTP {exc.response.status_code}",
                metadata={
                    "status_code": exc.response.status_code,
                    "latency_ms": latency_ms,
                },
            )
        except Exception as exc:  # noqa: BLE001 — network/timeout/parse
            latency_ms = int((time.monotonic() - started) * 1000)
            self._logger.warning(
                "admin.integrations.llm_check_failed",
                extra={
                    "event": "admin.integrations.llm_check_failed",
                    "error": str(exc),
                },
            )
            return IntegrationStatus(
                name=self.name,
                status=STATUS_UNHEALTHY,
                last_checked_at=_now(),
                error=f"LLM check failed: {exc}",
                metadata={"latency_ms": latency_ms},
            )

        latency_ms = int((time.monotonic() - started) * 1000)
        if not content:
            return IntegrationStatus(
                name=self.name,
                status=STATUS_DEGRADED,
                last_checked_at=_now(),
                error="LLM returned an empty completion",
                metadata={"latency_ms": latency_ms},
            )
        return IntegrationStatus(
            name=self.name,
            status=STATUS_HEALTHY,
            last_checked_at=_now(),
            error=None,
            metadata={"latency_ms": latency_ms, "response_chars": len(content)},
        )


class DatabaseChecker:
    """Health check for the SQLAlchemy database connection.

    The check opens a connection and runs ``SELECT 1``. A connection
    error (closed engine, wrong credentials, server down) is reported
    as ``unhealthy``; a successful round-trip is ``healthy``.
    """

    name: str = "database"

    def __init__(self, *, engine: Any) -> None:
        """Store the SQLAlchemy engine used for the probe.

        ``engine`` is typed as ``Any`` to avoid a hard dependency on
        the SQLAlchemy types in this module's public surface.
        """
        self._engine = engine
        self._logger = logging.getLogger(f"{_LOG_PREFIX}DatabaseChecker")

    async def check(self) -> IntegrationStatus:
        from sqlalchemy import text
        from sqlalchemy.exc import SQLAlchemyError

        started = time.monotonic()
        try:
            # The SQLAlchemy sync ``Engine.connect`` blocks briefly; we
            # run it in a thread so we don't stall the event loop. The
            # probe is cheap (a single ``SELECT 1``) so the offload is
            # acceptable.
            def _probe() -> None:
                with self._engine.connect() as connection:
                    connection.execute(text("SELECT 1"))

            await asyncio.to_thread(_probe)
        except SQLAlchemyError as exc:
            latency_ms = int((time.monotonic() - started) * 1000)
            return IntegrationStatus(
                name=self.name,
                status=STATUS_UNHEALTHY,
                last_checked_at=_now(),
                error=f"database ping failed: {exc}",
                metadata={"latency_ms": latency_ms},
            )
        except Exception as exc:  # noqa: BLE001 — engine closed, etc.
            latency_ms = int((time.monotonic() - started) * 1000)
            self._logger.warning(
                "admin.integrations.database_check_failed",
                extra={
                    "event": "admin.integrations.database_check_failed",
                    "error": str(exc),
                },
            )
            return IntegrationStatus(
                name=self.name,
                status=STATUS_UNHEALTHY,
                last_checked_at=_now(),
                error=f"database ping failed: {exc}",
                metadata={"latency_ms": latency_ms},
            )

        latency_ms = int((time.monotonic() - started) * 1000)
        return IntegrationStatus(
            name=self.name,
            status=STATUS_HEALTHY,
            last_checked_at=_now(),
            error=None,
            metadata={"latency_ms": latency_ms},
        )


# ---------------------------------------------------------------------------
# Worker
# ---------------------------------------------------------------------------


class IntegrationStatusWorker(BaseProcess):
    """Long-running :class:`BaseProcess` that refreshes the status store.

    The worker iterates over a fixed list of :class:`IntegrationChecker`
    instances on every :meth:`run_once` call and writes the result into
    the injected :class:`IntegrationStatusStore`. The :meth:`run`
    method drives the loop and uses the same shutdown machinery as
    :class:`~job_apply.features.apply_worker.runtime.ApplyWorkerProcess`
    so SIGINT/SIGTERM cleanly stop the loop on the next tick.
    """

    def __init__(
        self,
        *,
        store: IntegrationStatusStore,
        checkers: list[IntegrationChecker],
        refresh_interval_seconds: float = DEFAULT_REFRESH_INTERVAL_SECONDS,
        name: str = "integration-status-worker",
    ) -> None:
        if refresh_interval_seconds <= 0:
            raise ValueError("refresh_interval_seconds must be > 0")
        super().__init__(name=name)
        self._store = store
        # Copy so callers cannot mutate the registry after construction.
        self._checkers: list[IntegrationChecker] = list(checkers)
        self._refresh_interval_seconds = refresh_interval_seconds
        self._logger = logging.getLogger(f"{_LOG_PREFIX}IntegrationStatusWorker[{name}]")

    @property
    def store(self) -> IntegrationStatusStore:
        """Return the store the worker writes through."""
        return self._store

    @property
    def checkers(self) -> list[IntegrationChecker]:
        """Return a copy of the checker registry (read-only snapshot)."""
        return list(self._checkers)

    @property
    def refresh_interval_seconds(self) -> float:
        """Return the sleep duration between :meth:`run_once` calls."""
        return self._refresh_interval_seconds

    def request_shutdown(self) -> None:
        """Signal the worker to stop at the next opportunity.

        Thin wrapper over :meth:`BaseProcess.stop` that exists so the
        API tests do not have to reach into the parent class. The
        underlying event is the same one :meth:`run` polls.
        """
        self.stop()

    async def run_once(self) -> list[IntegrationStatus]:
        """Run every checker once and write the results to the store.

        A checker that raises is logged and recorded as
        ``STATUS_UNHEALTHY`` so one bad integration cannot break the
        rest of the loop. The return value is the list of statuses
        the store now holds for this iteration, in the same order as
        the :attr:`checkers` list — useful for callers that want to
        surface the freshest data without reading the store.
        """
        results: list[IntegrationStatus] = []
        for checker in self._checkers:
            try:
                status = await checker.check()
            except Exception as exc:  # noqa: BLE001 — isolate per-checker failures
                self._logger.exception(
                    "admin.integrations.checker_exception",
                    extra={
                        "event": "admin.integrations.checker_exception",
                        "checker": getattr(checker, "name", "unknown"),
                    },
                )
                status = IntegrationStatus(
                    name=getattr(checker, "name", "unknown"),
                    status=STATUS_UNHEALTHY,
                    last_checked_at=_now(),
                    error=f"checker raised: {exc}",
                    metadata=None,
                )
            self._store.update(status.name, status)
            results.append(status)
        return results

    async def run(self) -> int:
        """Drain the loop until the shutdown event is set.

        Returns 0 on a graceful shutdown. Exceptions raised by
        :meth:`run_once` are caught and logged so a single bad
        iteration cannot crash the worker. The shutdown event is
        observed on the next tick of the loop without waiting for
        the full refresh interval.
        """
        self.start()
        try:
            while not self.is_shutdown_set():
                try:
                    await self.run_once()
                except Exception:  # noqa: BLE001 — never crash the worker
                    self._logger.exception(
                        "admin.integrations.iteration_error",
                        extra={"event": "admin.integrations.iteration_error"},
                    )
                if self.is_shutdown_set():
                    break
                try:
                    await asyncio.wait_for(
                        self.wait_for_shutdown(),
                        timeout=self._refresh_interval_seconds,
                    )
                    break
                except TimeoutError:
                    pass
            return 0
        finally:
            self.stop()
