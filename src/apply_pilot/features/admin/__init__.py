"""Admin vertical slice (M6, issues #56, #57, #171).

This vertical slice exposes both read-only JSON views of system
health and the operator-facing HTML surface introduced in issue #171:

* ``GET  /admin/integrations``        — read-only view of the
  :class:`IntegrationStatusStore` (JSON).
* ``POST /admin/integrations/refresh`` — runs every registered
  :class:`IntegrationChecker` exactly once (JSON).
* ``GET  /admin/health``              — renders a thin HTML view of
  four system health facts.
* ``GET  /admin/``                    — operator landing page with
  nav links to every admin subpage (HTML, issue #171).
* ``GET  /admin/integrations``        — also serves an HTML table
  view (issue #171). The JSON view is content-negotiated.
* ``GET  /admin/users``               — paginated HTML table of every
  registered user (issue #171).

A long-running :class:`IntegrationStatusWorker`
(a :class:`~apply_pilot.runtime.process.BaseProcess` subclass) periodically
runs every :class:`IntegrationChecker` and updates the shared
:class:`InMemoryIntegrationStatusStore`. The ``/admin/health`` page
runs its probes inline via :class:`HealthCheck` and is self-contained —
it does not depend on the integrations worker.

Auth model
----------

Every admin route goes through
:func:`apply_pilot.features.admin._auth.require_admin_user` (JSON
endpoints) or :func:`resolve_admin_user` (HTML endpoints). Both
dependencies accept a bearer token (header) or the session cookie
introduced in PR #170, and both check the new ``is_admin`` flag
introduced in issue #171. The public signup endpoint deliberately
never sets ``is_admin``; the only way to bootstrap the first admin
is the ``apply-pilot promote --email <email>`` CLI.

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
* :data:`router` — FastAPI router for the JSON endpoints (mounted at
  ``/admin``).
* :data:`web_router` — FastAPI router for the HTML endpoints
  (mounted at ``/admin``; issue #171).
"""

from __future__ import annotations

from apply_pilot.features.admin._auth import require_admin_user, resolve_admin_user
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
from apply_pilot.features.admin.web import router as admin_web_router

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
    "admin_web_router",
    "get_health_checks",
    "require_admin_user",
    "resolve_admin_user",
    "router",
]
