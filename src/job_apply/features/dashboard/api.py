"""FastAPI router for the dashboard slice (M6, issue #51).

Single endpoint — ``GET /dashboard`` — returns a :class:`DashboardSummary`
for the authenticated user. All counts are scoped to the caller; a user
without any rows gets an all-zero response (with the embedded digest
``null`` when the service is built without a digest
:class:`StatsService`).
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from job_apply.db import get_db
from job_apply.features.apply_worker.repository import SqlApplyJobRepository
from job_apply.features.cover_letter.repository import SqlCoverLetterDraftRepository
from job_apply.features.dashboard.schemas import DashboardSummaryRead
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
    import uuid

    from job_apply.features.dashboard.schemas import dashboard_summary_to_read

    summary = service.get_summary(uuid.UUID(user_id_str))
    return dashboard_summary_to_read(summary)


__all__ = ["get_dashboard_service", "router"]
