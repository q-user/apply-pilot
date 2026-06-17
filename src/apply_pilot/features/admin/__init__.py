"""Admin vertical slice (M6, issues #56 and #57).

This vertical slice exposes two read-only views of system health:

* ``GET /admin/integrations`` returns the current health of every
  external integration (hh.ru OAuth, the LLM provider, the database,
  ...) and ``POST /admin/integrations/refresh`` manually triggers a
  one-shot refresh via the :class:`IntegrationStatusWorker`.
* ``GET /admin/health`` renders a thin HTML page of four system
  health facts (database reachable, redis reachable, LLM provider
  configured, current Alembic head).

A long-running :class:`IntegrationStatusWorker`
(a :class:`~apply_pilot.runtime.process.BaseProcess` subclass) periodically
runs every :class:`IntegrationChecker` and updates the shared
:class:`InMemoryIntegrationStatusStore`. The ``/admin/health`` page
runs its probes inline via :class:`HealthCheck` and is self-contained —
it does not depend on the integrations worker.

Public surface
--------------

* :class:`IntegrationStatus` — the in-process value object.
* :class:`InMemoryIntegrationStatusStore` — the default store.
* :class:`IntegrationStatusWorker` — the long-running
  :class:`BaseProcess` that drives the refresh loop.
* :class:`HhOAuthChecker`, :class:`LlmChecker`, :class:`DatabaseChecker`
  — the three concrete :class:`IntegrationChecker` implementations.
* :class:`HealthStatus` — status label enum.
* :class:`HealthCheckResult` — immutable probe result.
* :class:`HealthCheck` — Protocol every probe implements.
* :class:`DatabaseHealthCheck`, :class:`RedisHealthCheck`,
  :class:`LlmHealthCheck`, :class:`MigrationsHealthCheck` — the
  four concrete probes the page evaluates.
* :func:`get_health_checks` — FastAPI dependency returning the list
  of probes; tests override it to inject stubs.
* :data:`router` — FastAPI router (mounted at ``/admin``).
"""

from __future__ import annotations

from apply_pilot.features.admin.api import router
from apply_pilot.features.admin.health import (
    DatabaseHealthCheck,
    HealthCheck,
    HealthCheckResult,
    HealthStatus,
    LlmHealthCheck,
    MigrationsHealthCheck,
    RedisHealthCheck,
    get_health_checks,
)
from apply_pilot.features.admin.integrations import (
    DatabaseChecker,
    HhOAuthChecker,
    InMemoryIntegrationStatusStore,
    IntegrationChecker,
    IntegrationStatus,
    IntegrationStatusStore,
    IntegrationStatusWorker,
    LlmChecker,
)

__all__ = [
    "DatabaseChecker",
    "DatabaseHealthCheck",
    "HealthCheck",
    "HealthCheckResult",
    "HealthStatus",
    "HhOAuthChecker",
    "InMemoryIntegrationStatusStore",
    "IntegrationChecker",
    "IntegrationStatus",
    "IntegrationStatusStore",
    "IntegrationStatusWorker",
    "LlmChecker",
    "LlmHealthCheck",
    "MigrationsHealthCheck",
    "RedisHealthCheck",
    "get_health_checks",
    "router",
]
