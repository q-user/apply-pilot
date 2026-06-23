"""FastAPI router for the source-metrics slice (M7, issue #62).

The router exposes a single read-only endpoint:

* ``GET /admin/sources/metrics?source=<name>&since=<iso>&until=<iso>``
  — returns the recorded :class:`SourceMetricRead` events for the
  requested source, newest first, optionally bounded by an
  ISO-8601 ``since`` (strictly after) and ``until`` (inclusive)
  timestamp.

The endpoint is mounted under the ``/admin`` prefix so the OpenAPI
spec keeps the existing admin tag, and so the public spec does
not advertise per-source observability data to non-admin clients.
The repository is wired through FastAPI ``dependency_overrides``;
the default factory binds a fresh :class:`SqlSourceMetricRepository`
per request. The endpoint requires a valid bearer token (issue
#145); the gate honours the ``APP_ADMIN_REQUIRE_AUTH`` env flag (see
:mod:`apply_pilot.features.admin._auth`).
"""

from __future__ import annotations

import logging
from datetime import datetime

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from apply_pilot.db import get_db
from apply_pilot.features.admin._auth import require_admin_user
from apply_pilot.features.source_metrics.repository import (
    SourceMetricRepository,
    SqlSourceMetricRepository,
)
from apply_pilot.features.source_metrics.schemas import (
    SourceMetricRead,
    source_metric_event_to_read,
)

_LOGGER = logging.getLogger("apply_pilot.features.source_metrics.api")

router = APIRouter(prefix="/admin", tags=["admin"])


def get_source_metric_repository(
    session: Session = Depends(get_db),  # noqa: B008
) -> SourceMetricRepository:
    """Build a :class:`SourceMetricRepository` for the current request.

    Tests override this dependency to inject the in-memory fake.
    """
    return SqlSourceMetricRepository(session=session)


@router.get(
    "/sources/metrics",
    response_model=list[SourceMetricRead],
    responses={
        200: {"description": "Recorded metric events for the requested source."},
        401: {"description": "Missing or invalid bearer token."},
        422: {"description": "Missing or invalid query parameters."},
    },
    summary="List source ingest metric events",
)
def list_source_metrics(
    source: str = Query(  # noqa: B008
        ...,
        max_length=50,
        description="Filter by source identifier (e.g. ``hh``, ``habr``, ``telegram``).",
    ),
    since: datetime | None = Query(  # noqa: B008
        default=None,
        description="ISO 8601 datetime; only events with ``timestamp > since`` are returned.",
    ),
    until: datetime | None = Query(  # noqa: B008
        default=None,
        description="ISO 8601 datetime; only events with ``timestamp <= until`` are returned.",
    ),
    repo: SourceMetricRepository = Depends(get_source_metric_repository),  # noqa: B008
    _admin_user: str = Depends(require_admin_user),  # noqa: B008
) -> list[SourceMetricRead]:
    """Return recorded metric events for *source*, newest first.

    Both ``since`` and ``until`` are optional. ``since`` is a strict
    lower bound; ``until`` is an inclusive upper bound. The result
    is ordered by ``timestamp`` desc so the operator can see the
    freshest ingest at the top of the list.
    """
    events = repo.query(source_name=source, since=since, until=until)
    return [source_metric_event_to_read(event) for event in events]


__all__ = [
    "get_source_metric_repository",
    "list_source_metrics",
    "router",
]
