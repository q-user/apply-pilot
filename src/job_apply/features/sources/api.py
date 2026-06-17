"""FastAPI router for the sources slice (M6, issue #52).

Endpoints
---------

* ``GET /vacancies`` — paginated, filtered list of vacancies.

The endpoint is **public** (no bearer token required): the dashboard
and any future read-only client can list vacancies without
authentication. The slice does not yet need a write surface here —
ingest goes through the canonical :class:`SourceService` and is owned
by the M2 collector slices (hh, telegram).
"""

from __future__ import annotations

import logging
from datetime import datetime

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from job_apply.db import get_db
from job_apply.features.sources.repository import SqlVacancyRepository
from job_apply.features.sources.schemas import VacancyListResponse, VacancyRead
from job_apply.features.sources.service import SourceService, VacancyListResult

_LOGGER = logging.getLogger("job_apply.features.sources.api")

# No prefix: the route is exposed at the root as ``/vacancies`` per the
# M6 issue. Tagging the OpenAPI group as ``vacancies`` keeps the spec
# browsable; a future M6+ slice (e.g. ``/sources/<id>/ingest``) can
# sit next to it with its own tag without re-shaping this router.
router = APIRouter(tags=["vacancies"])


def get_vacancy_list_service(
    session: Session = Depends(get_db),  # noqa: B008
) -> SourceService:
    """Build a :class:`SourceService` for the current request.

    The service is reused for both ingest (later) and read here, so
    tests only need to override a single dependency to swap the
    repository backing.
    """
    repo = SqlVacancyRepository(session_factory=lambda: session)
    return SourceService(repository=repo)


# ---------------------------------------------------------------------------
# ORM → DTO mapper
# ---------------------------------------------------------------------------


def _to_dto(vacancy) -> VacancyRead:
    """Map a :class:`Vacancy` ORM row to the public DTO.

    Kept as a free function so tests can call it directly and so the
    route handler stays a one-liner.
    """
    return VacancyRead.model_validate(vacancy)


# ---------------------------------------------------------------------------
# Route handlers
# ---------------------------------------------------------------------------


@router.get(
    "/vacancies",
    response_model=VacancyListResponse,
    responses={
        422: {"description": "Invalid query parameters (e.g. limit > 100)"},
    },
    summary="List vacancies with optional filters and pagination.",
)
def list_vacancies(
    source: str | None = Query(  # noqa: B008
        default=None,
        max_length=50,
        description="Filter by source identifier (e.g. ``hh``, ``habr``, ``telegram``).",
    ),
    salary_min: int | None = Query(  # noqa: B008
        default=None,
        ge=0,
        description="Keep vacancies whose ``salary_from`` is at least this value.",
    ),
    location: str | None = Query(  # noqa: B008
        default=None,
        max_length=512,
        description="Case-insensitive substring match on the ``location`` field.",
    ),
    since: datetime | None = Query(  # noqa: B008
        default=None,
        description="ISO 8601 datetime; only vacancies with ``created_at > since`` are returned.",
    ),
    limit: int = Query(  # noqa: B008
        default=20,
        ge=1,
        le=100,
        description="Page size (1–100, default 20).",
    ),
    offset: int = Query(  # noqa: B008
        default=0,
        ge=0,
        description="Number of rows to skip before returning ``limit`` items.",
    ),
    service: SourceService = Depends(get_vacancy_list_service),  # noqa: B008
) -> VacancyListResponse:
    """Return a paginated list of vacancies matching the optional filter set.

    Filters combine as a logical AND; an omitted filter is "not
    applied". The response is ordered by ``created_at`` desc so the
    newest vacancies surface first. ``total`` reflects the full
    match count regardless of pagination, which lets the dashboard
    render a "page X of Y" indicator without a second round trip.
    """
    result: VacancyListResult = service.list_vacancies(
        source=source,
        salary_min=salary_min,
        location=location,
        since=since,
        limit=limit,
        offset=offset,
    )
    return VacancyListResponse(
        items=[_to_dto(v) for v in result.items],
        total=result.total,
        limit=limit,
        offset=offset,
    )


__all__ = [
    "get_vacancy_list_service",
    "router",
]
