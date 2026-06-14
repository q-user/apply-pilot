"""FastAPI router placeholder for the sources slice.

The full HTTP API (ingest endpoints, list, search) is delivered in a
follow-up issue. This module exposes the :data:`router` so the application
factory can register it and the OpenAPI surface can grow in a structured
way. The router currently advertises no routes on purpose.
"""

from __future__ import annotations

from fastapi import APIRouter

router = APIRouter(prefix="/sources", tags=["sources"])

__all__ = ["router"]
