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

All endpoints require a valid bearer token (issue #145). The auth
gate honours the ``APP_ADMIN_REQUIRE_AUTH`` env flag — when the flag
is ``true`` (the production default) the routes reject anonymous
requests with ``401``; operators that need to keep the legacy
open-access behaviour can flip the flag to ``false``. The
implementation lives in :mod:`apply_pilot.features.admin._auth`.
The router is mounted under the ``admin`` tag so the OpenAPI spec
stays browsable.
"""

from __future__ import annotations

import asyncio
import html
import logging
import re

from fastapi import APIRouter, Depends
from fastapi.responses import HTMLResponse

from apply_pilot.features.admin._auth import require_admin_user
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
        401: {"description": "Missing or invalid bearer token."},
    },
    summary="List integration health snapshots",
)
def list_integrations(
    store: IntegrationStatusStore = Depends(get_integration_status_store),  # noqa: B008
    _admin_user: str = Depends(require_admin_user),  # noqa: B008
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
        401: {"description": "Missing or invalid bearer token."},
        503: {"description": "No worker is registered; the refresh cannot run."},
    },
    summary="Trigger a one-shot integration refresh",
)
async def refresh_integrations(
    worker: IntegrationStatusWorker | None = Depends(get_integration_status_worker),  # noqa: B008
    _admin_user: str = Depends(require_admin_user),  # noqa: B008
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


# Patterns redacted from probe error messages before they reach the HTML
# page (issue #145, point 1). A raw ``str(exc)`` from a SQLAlchemy / redis
# / httpx exception can carry the DSN, bearer token, or env-var value
# verbatim — none of which belong in a public HTML page. The set of
# patterns is intentionally narrow: any well-known connection-string
# scheme, the ``Bearer`` auth scheme, and the canonical ``key=`` / ``password=``
# assignment shapes that drivers / SDKs use.
_REDACT_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"(?i)(?P<scheme>postgresql|postgres|mysql|sqlite|redis|amqp)://[^\s\"'<>]+"),
    re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._\-]+"),
    re.compile(
        r"(?i)\b(password|passwd|pwd|secret|api[_-]?key|access[_-]?token|token)\s*[=:]\s*[\S]+"
    ),
    re.compile(r"(?i)\b(\d{1,3}\.){3}\d{1,3}(?::\d+)?"),
)

_REDACTED = "[REDACTED]"


def _sanitize_error_message(message: str, *, limit: int = 240) -> str:
    """Strip secrets / connection strings from a probe error message.

    The output is safe to embed in the admin health page; a long, leaky
    original message is also truncated to ``limit`` characters so a
    multi-kilobyte stack-frame excerpt does not blow up the page.
    """
    if not message:
        return message
    redacted = message
    for pattern in _REDACT_PATTERNS:
        redacted = pattern.sub(_REDACTED, redacted)
    if len(redacted) > limit:
        redacted = redacted[:limit] + "..."
    return redacted


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
    responses={
        401: {"description": "Missing or invalid bearer token."},
    },
    summary="Render the admin health page",
)
async def admin_health_page(
    checks: list[HealthCheck] = Depends(get_health_checks),  # noqa: B008
    _admin_user: str = Depends(require_admin_user),  # noqa: B008
) -> HTMLResponse:
    """Render the admin health page (M6, issue #56).

    Each probe in *checks* is awaited sequentially. A failing probe
    surfaces as a row with status ``unhealthy`` (or ``unknown`` when
    the failure is "missing data", e.g. no ``alembic_version`` table);
    a successful probe surfaces as ``healthy``. The page itself
    never returns an error status code — the worst case is every
    row is ``unhealthy``, which is the operator's signal to act.

    When a probe raises, the rendered ``detail`` is sanitized: the
    exception class name replaces the raw ``str(exc)`` payload, and any
    connection strings / bearer tokens / ``password=`` / ``api_key=``
    assignments in the original message are replaced with
    ``[REDACTED]``. The unsanitized string is still emitted to the
    application log for operators.
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
                    detail=(
                        "probe raised: "
                        f"{outcome.__class__.__name__}: "
                        f"{_sanitize_error_message(str(outcome))}"
                    ),
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
