"""FastAPI application factory and entry point.

This module is the boundary between external HTTP traffic and the application
package. It deliberately avoids touching the database or Redis: the
``/healthz`` endpoint must be a pure, in-process liveness probe so that the
service can be checked even when the data plane is degraded.
"""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import uvicorn
from fastapi import FastAPI
from fastapi.responses import HTMLResponse

from apply_pilot.config import FastAPISettings, get_fastapi_settings

_LOGGER = logging.getLogger("apply_pilot.app")

# Minimal static landing page for the M6 frontend shell (issue #55).
# Kept as a module-level constant so the FastAPI handler stays cheap to
# call and the page can be rendered without any template engine.
_LANDING_HTML = """<!doctype html>
<html lang="en">
  <head>
    <meta charset="utf-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1" />
    <title>ApplyPilot</title>
    <style>
      :root {
        color-scheme: light dark;
        --bg: #f7f7f8;
        --fg: #1f2328;
        --muted: #57606a;
        --accent: #0969da;
        --card: #ffffff;
        --border: #d0d7de;
      }
      @media (prefers-color-scheme: dark) {
        :root {
          --bg: #0d1117;
          --fg: #e6edf3;
          --muted: #8b949e;
          --accent: #58a6ff;
          --card: #161b22;
          --border: #30363d;
        }
      }
      body {
        font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto,
          Oxygen, Ubuntu, Cantarell, "Helvetica Neue", sans-serif;
        background: var(--bg);
        color: var(--fg);
        margin: 0;
        padding: 0;
        line-height: 1.5;
      }
      main {
        max-width: 720px;
        margin: 0 auto;
        padding: 3rem 1.5rem;
      }
      h1 {
        font-size: 2.5rem;
        margin: 0 0 0.5rem;
        letter-spacing: -0.02em;
      }
      p.lede {
        color: var(--muted);
        font-size: 1.1rem;
        margin: 0 0 2rem;
      }
      ul.links {
        list-style: none;
        padding: 0;
        margin: 0;
        display: grid;
        gap: 0.75rem;
      }
      ul.links li a {
        display: block;
        padding: 1rem 1.25rem;
        background: var(--card);
        border: 1px solid var(--border);
        border-radius: 8px;
        color: var(--accent);
        text-decoration: none;
        font-weight: 500;
        transition: border-color 0.15s ease;
      }
      ul.links li a:hover {
        border-color: var(--accent);
      }
      ul.links li a small {
        display: block;
        color: var(--muted);
        font-weight: 400;
        margin-top: 0.25rem;
      }
      footer {
        margin-top: 3rem;
        color: var(--muted);
        font-size: 0.85rem;
      }
    </style>
  </head>
  <body>
    <main>
      <h1>ApplyPilot</h1>
      <p class="lede">
        A Telegram-first assistant for finding, scoring, reviewing, and applying
        to jobs. Browse the API documentation or jump into the operational
        surfaces below.
      </p>
      <ul class="links">
        <li>
          <a href="/docs">
            API docs (Swagger UI)
            <small>Interactive OpenAPI explorer for every endpoint.</small>
          </a>
        </li>
        <li>
          <a href="/redoc">
            API reference (ReDoc)
            <small>Clean, readable OpenAPI reference.</small>
          </a>
        </li>
        <li>
          <a href="/admin/health">
            Admin health
            <small>Service health, worker status, and dependency probes.</small>
          </a>
        </li>
        <li>
          <a href="/admin/scoring/experiments">
            Scoring experiments
            <small>A/B experiments and outcome aggregates for the scoring slice.</small>
          </a>
        </li>
        <li>
          <a href="/dashboard">
            Dashboard
            <small>Per-user summary of matches, applications, and digest.</small>
          </a>
        </li>
      </ul>
      <footer>
        ApplyPilot &middot; minimal frontend shell (M6).
      </footer>
    </main>
  </body>
</html>
"""


class _JsonFormatter(logging.Formatter):
    """Minimal JSON line formatter for structured logging."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": self.formatTime(record, self.default_time_format),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False)


def _configure_logging(settings: FastAPISettings) -> None:
    """Configure root logging once based on ``FastAPISettings``."""
    root = logging.getLogger()
    # Reset handlers so repeated calls (tests, reloads) don't stack formatters.
    for handler in list(root.handlers):
        root.removeHandler(handler)

    handler = logging.StreamHandler()
    if settings.log_json:
        handler.setFormatter(_JsonFormatter())
    else:
        handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s"))
    root.addHandler(handler)
    root.setLevel(settings.log_level)


def _build_lifespan(settings: FastAPISettings):
    """Return a lifespan context manager bound to ``settings``."""

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        _configure_logging(settings)
        _LOGGER.info(
            "app.startup",
            extra={"app_name": settings.app_name, "host": settings.host, "port": settings.port},
        )
        try:
            yield
        finally:
            _LOGGER.info("app.shutdown", extra={"app_name": settings.app_name})

    return lifespan


def create_app(settings: FastAPISettings | None = None) -> FastAPI:
    """Build a configured FastAPI application instance.

    ``settings`` is injectable so tests and alternative entry points can
    provide their own configuration without touching environment variables.
    """
    resolved = settings or get_fastapi_settings()
    app = FastAPI(
        title=resolved.app_name,
        lifespan=_build_lifespan(resolved),
    )

    # Register feature routers.
    from apply_pilot.features.admin import admin_web_router
    from apply_pilot.features.admin import router as admin_router
    from apply_pilot.features.apply_worker.api import (
        apply_history_router,
    )
    from apply_pilot.features.apply_worker.api import router as apply_worker_router
    from apply_pilot.features.cover_letter_style.api import (
        router as cover_letter_style_router,
    )
    from apply_pilot.features.dashboard.api import router as dashboard_router
    from apply_pilot.features.dashboard.web import router as dashboard_web_router
    from apply_pilot.features.hh.api import router as hh_router
    from apply_pilot.features.learning.api import router as learning_router
    from apply_pilot.features.matches.api import router as matches_router
    from apply_pilot.features.scoring_ab import router as scoring_ab_router
    from apply_pilot.features.scoring_review.api import router as scoring_review_router
    from apply_pilot.features.screening.api import router as screening_router
    from apply_pilot.features.search_profiles.api import router as search_profiles_router
    from apply_pilot.features.source_metrics.api import (
        router as source_metrics_router,
    )
    from apply_pilot.features.sources.api import router as sources_router
    from apply_pilot.features.telegram.digest.api import router as digest_router
    from apply_pilot.features.users.api import router as auth_router
    from apply_pilot.features.writing_style_memory.api import (
        router as writing_style_memory_router,
    )

    app.include_router(admin_web_router)
    app.include_router(admin_router)
    app.include_router(auth_router)
    app.include_router(apply_worker_router)
    app.include_router(apply_history_router)
    app.include_router(cover_letter_style_router)
    app.include_router(dashboard_router)
    app.include_router(dashboard_web_router)
    app.include_router(hh_router)
    app.include_router(learning_router)
    app.include_router(matches_router)
    app.include_router(scoring_ab_router)
    app.include_router(scoring_review_router)
    app.include_router(screening_router)
    app.include_router(search_profiles_router)
    app.include_router(sources_router)
    app.include_router(source_metrics_router)
    app.include_router(digest_router)
    app.include_router(writing_style_memory_router)

    @app.get("/healthz", include_in_schema=False)
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/", include_in_schema=False, response_class=HTMLResponse)
    async def landing() -> HTMLResponse:
        """Serve the minimal static HTML landing page (M6, issue #55)."""
        return HTMLResponse(content=_LANDING_HTML)

    return app


def run() -> None:
    """Entry point used by ``[project.scripts]`` and ``python -m apply_pilot.app``."""
    settings = get_fastapi_settings()
    uvicorn.run(
        "apply_pilot.app:create_app",
        factory=True,
        host=settings.host,
        port=settings.port,
        log_level=settings.log_level.lower(),
    )


if __name__ == "__main__":  # pragma: no cover - manual launch path
    run()
