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

from job_apply.config import FastAPISettings, get_fastapi_settings

_LOGGER = logging.getLogger("job_apply.app")


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
    from job_apply.features.apply_worker.api import router as apply_worker_router
    from job_apply.features.cover_letter_style.api import (
        router as cover_letter_style_router,
    )
    from job_apply.features.hh.api import router as hh_router
    from job_apply.features.matches.api import router as matches_router
    from job_apply.features.screening.api import router as screening_router
    from job_apply.features.search_profiles.api import router as search_profiles_router
    from job_apply.features.sources.api import router as sources_router
    from job_apply.features.telegram.digest.api import router as digest_router

    app.include_router(apply_worker_router)
    app.include_router(cover_letter_style_router)
    app.include_router(hh_router)
    app.include_router(matches_router)
    app.include_router(screening_router)
    app.include_router(search_profiles_router)
    app.include_router(sources_router)
    app.include_router(digest_router)

    @app.get("/healthz", include_in_schema=False)
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    return app


def run() -> None:
    """Entry point used by ``[project.scripts]`` and ``python -m job_apply.app``."""
    settings = get_fastapi_settings()
    uvicorn.run(
        "job_apply.app:create_app",
        factory=True,
        host=settings.host,
        port=settings.port,
        log_level=settings.log_level.lower(),
    )


if __name__ == "__main__":  # pragma: no cover - manual launch path
    run()
