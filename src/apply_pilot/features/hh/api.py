"""FastAPI router for the public HH read slice.

Endpoints
---------

* ``GET /hh/search`` — search vacancies on hh.ru (public, no auth).

The HH OAuth flow and apply-via-API have been removed in M10. Apply
is delegated to a separate headless-browser tool (see issue #206).
"""

from __future__ import annotations

from fastapi import APIRouter

router = APIRouter(prefix="/hh", tags=["hh"])
