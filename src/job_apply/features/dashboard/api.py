"""FastAPI router for the dashboard slice (M6, issue #51 + M8, #67).

Four endpoints share the ``/dashboard`` prefix:

* ``GET /dashboard``             — per-user :class:`DashboardSummary`
                                  (flat totals).
* ``GET /dashboard/funnel``      — per-source funnel counts
                                  (issue #67).
* ``GET /dashboard/conversion``  — per-profile conversion rates
                                  (issue #67).
* ``GET /dashboard/time-to-apply`` — average + median wall-clock
                                  seconds from :class:`VacancyMatch`
                                  to :class:`ApplyJob` for terminal
                                  jobs (issue #67).

All counts are scoped to the caller; a user without any rows gets
an all-zero / empty / ``null`` response. Every endpoint requires a
bearer token.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, Query, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from job_apply.db import get_db
from job_apply.features.apply_worker.repository import SqlApplyJobRepository
from job_apply.features.cover_letter.repository import SqlCoverLetterDraftRepository
from job_apply.features.dashboard.schemas import (
    ConversionRead,
    DashboardSummaryRead,
    FunnelRead,
    TimeToApplyRead,
    conversion_to_read,
    dashboard_summary_to_read,
    funnel_to_read,
    time_to_apply_to_read,
)
from job_apply.features.dashboard.service import DashboardService
from job_apply.features.matches.repository import SqlVacancyMatchRepository
from job_apply.features.search_profiles.repository import SqlSearchProfileRepository
from job_apply.features.sources.repository import SqlVacancyRepository
from job_apply.features.telegram.repository import SqlAlchemyTelegramAccountRepository
from job_apply.features.users.repository import SqlAlchemyUsersRepository
from job_apply.features.users.security import InvalidTokenError, default_token_store

_LOGGER = logging.getLogger("job_apply.features.dashboard.api")

router = APIRouter(prefix="/dashboard", tags=["dashboard"])

_bearer_scheme = HTTPBearer(auto_error=False)


def _http_error(status_code: int, code: str, message: str) -> Exception:
    """Return a JSON-shaped 4xx error that the API contract promises."""
    from fastapi import HTTPException

    return HTTPException(status_code=status_code, detail={"code": code, "message": message})


def _resolve_user_id(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer_scheme),  # noqa: B008
) -> str:
    """Extract the user id from the bearer token, or raise 401."""
    if credentials is None:
        raise _http_error(
            status.HTTP_401_UNAUTHORIZED,
            "authentication_required",
            "bearer token is required",
        )
    tokens = default_token_store()
    try:
        return tokens.resolve(credentials.credentials)
    except InvalidTokenError as exc:
        raise _http_error(
            status.HTTP_401_UNAUTHORIZED,
            "invalid_token",
            "the supplied token is invalid or expired",
        ) from exc


def get_dashboard_service(
    session: Session = Depends(get_db),  # noqa: B008
) -> DashboardService:
    """Build a :class:`DashboardService` for the current request.

    All seven repositories share the request-scoped session so the
    dashboard read is consistent. The service builds the
    :class:`StatsService` lazily on the first :meth:`get_summary` call.
    """
    match_repo = SqlVacancyMatchRepository(session_factory=lambda: session)
    apply_job_repo = SqlApplyJobRepository(session_factory=lambda: session)
    cover_letter_repo = SqlCoverLetterDraftRepository(session=session)
    vacancy_repo = SqlVacancyRepository(session_factory=lambda: session)
    profile_repo = SqlSearchProfileRepository(session_factory=lambda: session)
    telegram_repo = SqlAlchemyTelegramAccountRepository(session=session)
    user_repo = SqlAlchemyUsersRepository(session=session)
    return DashboardService(
        match_repo=match_repo,
        apply_job_repo=apply_job_repo,
        cover_letter_repo=cover_letter_repo,
        vacancy_repo=vacancy_repo,
        profile_repo=profile_repo,
        telegram_account_repo=telegram_repo,
        user_repo=user_repo,
    )


# ---------------------------------------------------------------------------
# Route handler
# ---------------------------------------------------------------------------


@router.get(
    "",
    response_model=DashboardSummaryRead,
    responses={
        401: {"description": "Missing or invalid bearer token"},
    },
)
def get_dashboard(
    user_id_str: str = Depends(_resolve_user_id),  # noqa: B008
    service: DashboardService = Depends(get_dashboard_service),  # noqa: B008
) -> DashboardSummaryRead:
    """Return a :class:`DashboardSummary` for the authenticated user.

    The endpoint never returns user-A data to user-B: every count is
    derived from a repository call scoped to the caller's user id.
    A user without any rows gets an all-zero response with an
    embedded (all-zero) digest.
    """
    summary = service.get_summary(uuid.UUID(user_id_str))
    return dashboard_summary_to_read(summary)


# ---------------------------------------------------------------------------
# M8 analytics endpoints (issue #67)
# ---------------------------------------------------------------------------


@router.get(
    "/funnel",
    response_model=FunnelRead,
    responses={
        401: {"description": "Missing or invalid bearer token"},
        422: {"description": "Invalid query parameters"},
    },
)
def get_dashboard_funnel(
    user_id_str: str = Depends(_resolve_user_id),  # noqa: B008
    service: DashboardService = Depends(get_dashboard_service),  # noqa: B008
    source: str | None = Query(  # noqa: B008
        default=None,
        description="Restrict the funnel to a single source (e.g. 'hh', 'habr').",
    ),
    since: datetime | None = Query(  # noqa: B008
        default=None,
        description="Lower bound (inclusive) for vacancies.created_at and matches.created_at.",
    ),
    until: datetime | None = Query(  # noqa: B008
        default=None,
        description="Upper bound (exclusive) for vacancies.created_at and matches.created_at.",
    ),
) -> FunnelRead:
    """Return the per-source funnel for the authenticated user.

    The response carries one :class:`FunnelRow` per source, plus a
    ``filters`` echo of the input parameters so the front-end can
    confirm what the user queried.
    """
    rows = service.get_funnel(
        uuid.UUID(user_id_str),
        source=source,
        since=since,
        until=until,
    )
    return funnel_to_read(rows, source=source, since=since, until=until)


@router.get(
    "/conversion",
    response_model=ConversionRead,
    responses={
        401: {"description": "Missing or invalid bearer token"},
        422: {"description": "Invalid query parameters"},
    },
)
def get_dashboard_conversion(
    user_id_str: str = Depends(_resolve_user_id),  # noqa: B008
    service: DashboardService = Depends(get_dashboard_service),  # noqa: B008
    profile_id: uuid.UUID | None = Query(  # noqa: B008
        default=None,
        description="Restrict the conversion table to a single search profile.",
    ),
) -> ConversionRead:
    """Return the per-profile conversion table for the authenticated user.

    Each :class:`ConversionRow` carries the matches / accepted /
    applied counts plus the two rates. ``rows`` is empty when the
    user owns no profiles (or when ``profile_id`` does not match).
    """
    rows = service.get_conversion(uuid.UUID(user_id_str), profile_id=profile_id)
    return conversion_to_read(rows)


@router.get(
    "/time-to-apply",
    response_model=TimeToApplyRead | None,
    responses={
        401: {"description": "Missing or invalid bearer token"},
        422: {"description": "Invalid query parameters"},
    },
)
def get_dashboard_time_to_apply(
    user_id_str: str = Depends(_resolve_user_id),  # noqa: B008
    service: DashboardService = Depends(get_dashboard_service),  # noqa: B008
    source: str | None = Query(  # noqa: B008
        default=None,
        description="Restrict the metric to matches whose vacancy.source matches.",
    ),
    profile_id: uuid.UUID | None = Query(  # noqa: B008
        default=None,
        description="Restrict the metric to matches owned by this search profile.",
    ),
) -> TimeToApplyRead | None:
    """Return the average + median time-to-apply for the authenticated user.

    The metric is the wall-clock delta between
    :attr:`VacancyMatch.created_at` and
    :attr:`ApplyJob.finished_at` for every terminal-state apply job
    the user owns. The response is JSON ``null`` when no data is
    available (so the front-end can render a placeholder without
    a presence check).
    """
    stats = service.get_time_to_apply(uuid.UUID(user_id_str), source=source, profile_id=profile_id)
    return time_to_apply_to_read(stats)


__all__ = ["get_dashboard_service", "router"]
