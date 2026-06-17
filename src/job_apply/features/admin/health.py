"""Admin health slice (M6, issue #56).

A thin read-only view of system health. The slice owns:

* :class:`HealthStatus` — small enum of status labels.
* :class:`HealthCheckResult` — immutable value object the page renders.
* :class:`HealthCheck` — Protocol every concrete probe implements.
* Four concrete checks (database, redis, LLM, migrations) that probe
  the corresponding dependency and return a :class:`HealthCheckResult`.
* :func:`get_health_checks` — FastAPI dependency that returns the list
  of checks to evaluate for the current request. Tests override this
  dependency to stub the probes without touching real infrastructure.

The slice is intentionally self-contained: it does not import the
``admin.integrations`` module from issue #57 (which is still in
flight) and does not depend on the worker process. If / when that
slice is merged, the page can be upgraded to read from
``IntegrationStatusStore`` instead of running the probes inline.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
from dataclasses import dataclass
from enum import Enum
from typing import Any, Protocol, runtime_checkable

from job_apply.config import Settings
from job_apply.db import engine
from job_apply.runtime import create_redis_client

_LOGGER = logging.getLogger("job_apply.features.admin.health")

#: Name of the Alembic bookkeeping table that stores the current head.
ALEMBIC_VERSION_TABLE: str = "alembic_version"

#: Name of the column that stores the current head revision.
ALEMBIC_VERSION_COLUMN: str = "version_num"


class HealthStatus(str, Enum):
    """Status labels the admin health page renders.

    Using :class:`str` + :class:`Enum` so the value is JSON-serializable
    and renders cleanly in the HTML template (no extra ``.value`` access).
    """

    HEALTHY = "healthy"
    DEGRADED = "degraded"
    UNHEALTHY = "unhealthy"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class HealthCheckResult:
    """Immutable snapshot of a single health probe.

    Attributes:
        name: Stable identifier of the probe (e.g. ``"database"``).
        status: One of the :class:`HealthStatus` values.
        detail: Human-readable detail. On success, typically the
            measured value (``"ping ok"``, ``"head=abc123"``); on
            failure, the error message.
    """

    name: str
    status: HealthStatus
    detail: str = ""


@runtime_checkable
class HealthCheck(Protocol):
    """Contract every health probe implements.

    A probe exposes a stable ``name`` and an async ``run`` coroutine
    that returns a :class:`HealthCheckResult`. The page calls ``run``
    sequentially for every probe; a slow probe only delays its own
    row, not the others (the page renders the result row as soon as
    it is available).
    """

    name: str

    async def run(self) -> HealthCheckResult:
        """Run the probe and return the result."""
        ...


# ---------------------------------------------------------------------------
# Concrete checks
# ---------------------------------------------------------------------------


class DatabaseHealthCheck:
    """Probe the SQLAlchemy engine with a cheap ``SELECT 1``.

    The probe is sync; we offload it to a worker thread so the event
    loop is not blocked when the underlying engine takes a few hundred
    milliseconds to acquire a connection.
    """

    name: str = "database"

    def __init__(self, *, engine: Any = None) -> None:
        self._engine = engine

    async def run(self) -> HealthCheckResult:
        from sqlalchemy import text
        from sqlalchemy.exc import SQLAlchemyError

        eng = self._engine if self._engine is not None else engine

        def _probe() -> None:
            with eng.connect() as connection:
                connection.execute(text("SELECT 1"))

        try:
            await asyncio.to_thread(_probe)
        except SQLAlchemyError as exc:
            return HealthCheckResult(
                name=self.name,
                status=HealthStatus.UNHEALTHY,
                detail=f"ping failed: {exc}",
            )
        except Exception as exc:  # noqa: BLE001 — engine closed, import errors, etc.
            _LOGGER.warning(
                "admin.health.database_probe_failed",
                extra={"event": "admin.health.database_probe_failed", "error": str(exc)},
            )
            return HealthCheckResult(
                name=self.name,
                status=HealthStatus.UNHEALTHY,
                detail=f"ping failed: {exc}",
            )
        return HealthCheckResult(name=self.name, status=HealthStatus.HEALTHY, detail="ping ok")


class RedisHealthCheck:
    """Probe Redis with ``PING``.

    The probe is async; the underlying ``redis.asyncio.Redis`` is
    built from the standard :class:`Settings` so the URL, db, and
    password come from the environment, mirroring production wiring.
    """

    name: str = "redis"

    def __init__(self, *, settings: Settings | None = None) -> None:
        self._settings = settings

    async def run(self) -> HealthCheckResult:
        # Re-create the client per probe — PING is cheap and a stale
        # client would not detect a server restart.
        client = create_redis_client(self._settings or _default_settings())
        try:
            pong = await client.ping()
        except Exception as exc:  # noqa: BLE001 — connection refused, auth, etc.
            return HealthCheckResult(
                name=self.name,
                status=HealthStatus.UNHEALTHY,
                detail=f"ping failed: {exc}",
            )
        finally:
            # Best-effort close — the client is per-probe and we never
            # want a lingering socket to mask the actual probe result.
            with contextlib.suppress(Exception):
                await client.aclose()
        status = HealthStatus.HEALTHY if pong else HealthStatus.UNHEALTHY
        detail = "ping ok" if pong else f"ping returned {pong!r}"
        return HealthCheckResult(name=self.name, status=status, detail=detail)


class LlmHealthCheck:
    """Probe whether the LLM provider is configured.

    The check does not hit the network — the slice contract is
    "LLM provider configured (boolean from config)". We treat an
    unset ``APP_LLM_API_KEY`` as ``unhealthy`` because the M3 scoring
    pipeline is a hard dependency of the cover-letter and
    review-match flows; the operator must see this on the health
    page even when the network is up.
    """

    name: str = "llm"

    async def run(self) -> HealthCheckResult:
        api_key = os.getenv("APP_LLM_API_KEY", "").strip()
        model = os.getenv("APP_LLM_MODEL", "").strip() or "gpt-4o-mini"
        if not api_key:
            return HealthCheckResult(
                name=self.name,
                status=HealthStatus.UNHEALTHY,
                detail="APP_LLM_API_KEY is not set",
            )
        return HealthCheckResult(
            name=self.name,
            status=HealthStatus.HEALTHY,
            detail=f"model={model}",
        )


class MigrationsHealthCheck:
    """Probe the current Alembic head from the ``alembic_version`` table.

    The probe opens a read-only connection and runs
    ``SELECT version_num FROM alembic_version``. If the table does
    not exist (typical for a fresh sqlite test DB) we report
    ``unknown`` rather than 5xx-ing the page.
    """

    name: str = "migrations"

    def __init__(self, *, engine: Any = None) -> None:
        self._engine = engine

    async def run(self) -> HealthCheckResult:
        from sqlalchemy import text
        from sqlalchemy.exc import DBAPIError, SQLAlchemyError

        eng = self._engine if self._engine is not None else engine

        def _probe() -> str | None:
            with eng.connect() as connection:
                row = connection.execute(
                    text(f"SELECT {ALEMBIC_VERSION_COLUMN} FROM {ALEMBIC_VERSION_TABLE}")
                ).first()
            if row is None:
                return None
            return str(row[0])

        try:
            head = await asyncio.to_thread(_probe)
        except DBAPIError:
            # Table does not exist — SQLAlchemy raises ``OperationalError``
            # (subclass of ``DBAPIError``) on SQLite and ``ProgrammingError``
            # (also a ``DBAPIError``) on PostgreSQL. Both mean the same
            # thing: no ``alembic_version`` table, so treat as "no
            # migrations applied".
            return HealthCheckResult(
                name=self.name,
                status=HealthStatus.UNKNOWN,
                detail=f"{ALEMBIC_VERSION_TABLE} table not found",
            )
        except SQLAlchemyError as exc:
            return HealthCheckResult(
                name=self.name,
                status=HealthStatus.UNHEALTHY,
                detail=f"head lookup failed: {exc}",
            )
        except Exception as exc:  # noqa: BLE001
            _LOGGER.warning(
                "admin.health.migrations_probe_failed",
                extra={"event": "admin.health.migrations_probe_failed", "error": str(exc)},
            )
            return HealthCheckResult(
                name=self.name,
                status=HealthStatus.UNHEALTHY,
                detail=f"head lookup failed: {exc}",
            )
        if head is None:
            return HealthCheckResult(
                name=self.name,
                status=HealthStatus.UNKNOWN,
                detail="no head recorded",
            )
        return HealthCheckResult(
            name=self.name,
            status=HealthStatus.HEALTHY,
            detail=f"head={head}",
        )


# ---------------------------------------------------------------------------
# Dependency factory
# ---------------------------------------------------------------------------


def get_health_checks() -> list[HealthCheck]:
    """Return the default list of probes for the admin health page.

    The order of the list is the order the page renders rows in, so
    keep it stable (database, redis, llm, migrations) — the same
    order operators see in the project docs and the issue spec.
    """
    return [
        DatabaseHealthCheck(),
        RedisHealthCheck(),
        LlmHealthCheck(),
        MigrationsHealthCheck(),
    ]


def _default_settings() -> Settings:
    """Build the :class:`Settings` the default Redis probe uses.

    The probe normally picks up its URL from the live env, but the
    factory signature requires a :class:`Settings` object — keep a
    single place that reads from the environment so the value is
    consistent with :func:`job_apply.config.get_settings`.
    """
    from job_apply.config import get_settings

    return get_settings()


__all__ = [
    "ALEMBIC_VERSION_COLUMN",
    "ALEMBIC_VERSION_TABLE",
    "DatabaseHealthCheck",
    "HealthCheck",
    "HealthCheckResult",
    "HealthStatus",
    "LlmHealthCheck",
    "MigrationsHealthCheck",
    "RedisHealthCheck",
    "get_health_checks",
]
