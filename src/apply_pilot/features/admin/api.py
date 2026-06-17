"""FastAPI router for the admin vertical slice (M6, issues #56 and #57).

The router is mounted at ``/admin`` and exposes:

* ``GET  /admin/integrations``        — read-only view of the
  :class:`IntegrationStatusStore`. The endpoint is a thin wrapper
  around :meth:`store.get_all` and never blocks on the network.
* ``POST /admin/integrations/refresh`` — runs every registered
  :class:`IntegrationChecker` exactly once and returns the freshly
  refreshed list. Useful for operator-driven ``curl`` checks.
* ``GET  /admin/health``              — renders a thin HTML view of
  four system health facts (database reachable, redis reachable,
  LLM provider configured, current Alembic head).

All endpoints are intentionally unauthenticated for now — the M6
contract is "admin worker + integration status view + admin health
page", and the authorization story is tracked separately. The router
is mounted under the ``admin`` tag so the OpenAPI spec stays browsable.
"""

from __future__ import annotations

import asyncio
import html
import logging

from fastapi import APIRouter, Depends
from fastapi.responses import HTMLResponse

from apply_pilot.features.admin.health import (
    HealthCheck,
    HealthCheckResult,
    HealthStatus,
    get_health_checks,
)
from apply_pilot.features.admin.integrations import (
    InMemoryIntegrationStatusStore,
    IntegrationStatusStore,
    IntegrationStatusWorker,
)
from apply_pilot.features.admin.schemas import (
    IntegrationStatusRead,
    integration_status_to_read,
)

_LOGGER = logging.getLogger("apply_pilot.features.admin.api")

router = APIRouter(prefix="/admin", tags=["admin"])


# ---------------------------------------------------------------------------
# Module-level singletons (integration status slice, issue #57)
# ---------------------------------------------------------------------------
#
# The integration slice is a single-process MVP. The store and the
# worker are module-level so the API dependency, the long-running
# worker process, and the manual ``POST /integrations/refresh`` endpoint
# all see the same state. Tests override the dependencies to inject fakes.
_default_store: InMemoryIntegrationStatusStore = InMemoryIntegrationStatusStore()
_default_worker: IntegrationStatusWorker | None = None


def configure_default_worker(worker: IntegrationStatusWorker) -> None:
    """Register the long-running worker used by ``POST /integrations/refresh``.

    Production wiring calls this once at app startup. The dependency
    factory :func:`get_integration_status_worker` returns the
    configured instance, or ``None`` if no worker has been registered
    yet (e.g. during early startup or in tests).
    """
    global _default_worker
    _default_worker = worker
    _LOGGER.info(
        "admin.integrations.worker_configured",
        extra={
            "event": "admin.integrations.worker_configured",
            "checkers": [getattr(c, "name", "?") for c in worker.checkers],
        },
    )


# ---------------------------------------------------------------------------
# Dependency factories (integration status slice, issue #57)
# ---------------------------------------------------------------------------


def get_integration_status_store() -> IntegrationStatusStore:
    """Return the :class:`IntegrationStatusStore` shared with the worker.

    The default is the module-level :data:`_default_store`. Tests
    override this dependency to inject a fresh store.
    """
    return _default_store


def get_integration_status_worker() -> IntegrationStatusWorker | None:
    """Return the configured worker, or ``None`` if not yet registered.

    The ``POST /integrations/refresh`` endpoint uses this to trigger a
    manual refresh; the ``GET`` endpoint does not depend on it. Returning
    ``None`` instead of raising keeps the GET endpoint functional
    even before the worker is up.
    """
    return _default_worker


# ---------------------------------------------------------------------------
# Route handlers — integration status (issue #57)
# ---------------------------------------------------------------------------


@router.get(
    "/integrations",
    response_model=list[IntegrationStatusRead],
    responses={
        200: {"description": "Current status of every known integration."},
    },
    summary="List integration health snapshots",
)
def list_integrations(
    store: IntegrationStatusStore = Depends(get_integration_status_store),  # noqa: B008
) -> list[IntegrationStatusRead]:
    """Return the cached :class:`IntegrationStatus` for every integration.

    The endpoint reads the in-process store populated by
    :class:`IntegrationStatusWorker`. When the worker has not run yet
    the response is an empty list — the slice contract deliberately
    avoids inventing fake "unknown" entries so operators can tell at
    a glance whether the worker is alive.
    """
    return [integration_status_to_read(status) for status in store.get_all()]


@router.post(
    "/integrations/refresh",
    response_model=list[IntegrationStatusRead],
    responses={
        200: {"description": "Statuses after a one-shot refresh."},
        503: {"description": "No worker is registered; the refresh cannot run."},
    },
    summary="Trigger a one-shot integration refresh",
)
async def refresh_integrations(
    worker: IntegrationStatusWorker | None = Depends(get_integration_status_worker),  # noqa: B008
) -> list[IntegrationStatusRead]:
    """Run every checker once and return the freshest statuses.

    A missing worker is reported as ``503`` so the operator knows
    the refresh did not actually run (rather than silently returning
    the stale cache).
    """
    if worker is None:
        from fastapi import HTTPException
        from fastapi import status as _status

        raise HTTPException(
            status_code=_status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "code": "integration_worker_not_configured",
                "message": "no integration status worker is registered for this process",
            },
        )
    results = await worker.run_once()
    return [integration_status_to_read(status) for status in results]


# ---------------------------------------------------------------------------
# Route handlers — admin health page (issue #56)
# ---------------------------------------------------------------------------
#
# Inline HTML template (Jinja2 is intentionally not added as a
# dependency for the M6 admin slice; the page is tiny and the
# landing page already uses the same inline-HTML pattern).


_HEALTH_PAGE_HTML = """<!doctype html>
<html lang="en">
  <head>
    <meta charset="utf-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1" />
    <title>Admin health · ApplyPilot</title>
    <style>
      :root {{
        color-scheme: light dark;
        --bg: #f7f7f8;
        --fg: #1f2328;
        --muted: #57606a;
        --card: #ffffff;
        --border: #d0d7de;
        --ok: #1a7f37;
        --warn: #9a6700;
        --bad: #cf222e;
        --neutral: #57606a;
      }}
      @media (prefers-color-scheme: dark) {{
        :root {{
          --bg: #0d1117;
          --fg: #e6edf3;
          --muted: #8b949e;
          --card: #161b22;
          --border: #30363d;
          --ok: #3fb950;
          --warn: #d29922;
          --bad: #f85149;
          --neutral: #8b949e;
        }}
      }}
      body {{
        font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto,
          Oxygen, Ubuntu, Cantarell, "Helvetica Neue", sans-serif;
        background: var(--bg);
        color: var(--fg);
        margin: 0;
        padding: 0;
        line-height: 1.5;
      }}
      main {{
        max-width: 720px;
        margin: 0 auto;
        padding: 3rem 1.5rem;
      }}
      header {{
        display: flex;
        justify-content: space-between;
        align-items: baseline;
        margin-bottom: 2rem;
      }}
      header h1 {{
        font-size: 2rem;
        margin: 0;
        letter-spacing: -0.02em;
      }}
      header a {{
        color: var(--muted);
        font-size: 0.9rem;
        text-decoration: none;
      }}
      header a:hover {{
        text-decoration: underline;
      }}
      ul.probes {{
        list-style: none;
        padding: 0;
        margin: 0;
        display: grid;
        gap: 0.75rem;
      }}
      ul.probes li {{
        background: var(--card);
        border: 1px solid var(--border);
        border-radius: 8px;
        padding: 1rem 1.25rem;
      }}
      ul.probes .row {{
        display: flex;
        justify-content: space-between;
        align-items: baseline;
        gap: 1rem;
      }}
      ul.probes .name {{
        font-weight: 600;
        font-size: 1.05rem;
      }}
      ul.probes .status {{
        font-size: 0.85rem;
        font-weight: 500;
        text-transform: uppercase;
        letter-spacing: 0.05em;
      }}
      ul.probes .status.healthy {{ color: var(--ok); }}
      ul.probes .status.degraded {{ color: var(--warn); }}
      ul.probes .status.unhealthy {{ color: var(--bad); }}
      ul.probes .status.unknown {{ color: var(--neutral); }}
      ul.probes .detail {{
        margin-top: 0.5rem;
        color: var(--muted);
        font-size: 0.9rem;
        font-family: ui-monospace, SFMono-Regular, "SF Mono", Menlo,
          Consolas, "Liberation Mono", monospace;
        word-break: break-word;
      }}
      ul.probes .detail.empty {{
        color: var(--muted);
        font-style: italic;
      }}
    </style>
  </head>
  <body>
    <main>
      <header>
        <h1>Admin health</h1>
        <a href="/">&larr; Back to home</a>
      </header>
      <ul class="probes">
        {rows}
      </ul>
    </main>
  </body>
</html>
"""


_PROBE_LABELS: dict[str, str] = {
    "database": "Database",
    "redis": "Redis",
    "llm": "LLM",
    "migrations": "Migrations",
}


def _format_row(result: HealthCheckResult) -> str:
    """Render a single :class:`HealthCheckResult` as a list item."""
    safe_name = html.escape(_PROBE_LABELS.get(result.name, result.name.title()))
    safe_status = html.escape(result.status.value)
    detail_class = "detail empty" if not result.detail else "detail"
    safe_detail = html.escape(result.detail) if result.detail else "no detail"
    return (
        f'        <li data-probe="{html.escape(result.name)}">\n'
        f'          <div class="row">\n'
        f'            <span class="name">{safe_name}</span>\n'
        f'            <span class="status {safe_status}">{safe_status}</span>\n'
        f"          </div>\n"
        f'          <div class="{detail_class}">{safe_detail}</div>\n'
        f"        </li>"
    )


def _render(results: list[HealthCheckResult]) -> str:
    """Render the list of results as the inner HTML of the page."""
    if not results:
        return '        <li><div class="row"><span class="name">No probes</span></div></li>'
    return "\n".join(_format_row(result) for result in results)


@router.get(
    "/health",
    response_class=HTMLResponse,
    include_in_schema=True,
    summary="Render the admin health page",
)
async def admin_health_page(
    checks: list[HealthCheck] = Depends(get_health_checks),  # noqa: B008
) -> HTMLResponse:
    """Render the admin health page (M6, issue #56).

    Each probe in *checks* is awaited sequentially. A failing probe
    surfaces as a row with status ``unhealthy`` (or ``unknown`` when
    the failure is "missing data", e.g. no ``alembic_version`` table);
    a successful probe surfaces as ``healthy``. The page itself
    never returns an error status code — the worst case is every
    row is ``unhealthy``, which is the operator's signal to act.
    """
    coros = [check.run() for check in checks]
    results = await asyncio.gather(*coros, return_exceptions=True)

    rendered: list[HealthCheckResult] = []
    for check, outcome in zip(checks, results, strict=True):
        if isinstance(outcome, BaseException):
            _LOGGER.warning(
                "admin.health.probe_raised",
                extra={
                    "event": "admin.health.probe_raised",
                    "probe": check.name,
                    "error": str(outcome),
                },
            )
            rendered.append(
                HealthCheckResult(
                    name=check.name,
                    status=HealthStatus.UNHEALTHY,
                    detail=f"probe raised: {outcome}",
                )
            )
        else:
            rendered.append(outcome)

    return HTMLResponse(content=_HEALTH_PAGE_HTML.format(rows=_render(rendered)))


__all__ = [
    "admin_health_page",
    "configure_default_worker",
    "get_integration_status_store",
    "get_integration_status_worker",
    "list_integrations",
    "refresh_integrations",
    "router",
]
