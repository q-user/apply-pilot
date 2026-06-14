"""FastAPI router for the sources slice.

Endpoints
---------

* ``POST /sources/ingest`` — ingest a vacancy from external raw data.

The router is registered in ``app.py``. All endpoints require a valid
bearer token for now; the ingest endpoint will later be called by
background workers or webhook handlers.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from job_apply.db import get_db
from job_apply.features.sources.repository import SqlVacancyRepository
from job_apply.features.sources.service import SourceService
from job_apply.features.users.security import InvalidTokenError, default_token_store

_LOGGER = logging.getLogger("job_apply.features.sources.api")

router = APIRouter(prefix="/sources", tags=["sources"])

_bearer_scheme = HTTPBearer(auto_error=False)


def _http_error(status_code: int, code: str, message: str) -> HTTPException:
    return HTTPException(status_code=status_code, detail={"code": code, "message": message})


def _resolve_user_id(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer_scheme),  # noqa: B008
) -> str:
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


def get_source_service(
    session: Session = Depends(get_db),  # noqa: B008
) -> SourceService:
    """Build a ``SourceService`` for the current request."""
    repo = SqlVacancyRepository(session_factory=lambda: session)
    return SourceService(repo)


@router.post(
    "/ingest",
    status_code=status.HTTP_201_CREATED,
    responses={
        401: {"description": "Missing or invalid bearer token"},
        422: {"description": "Validation error"},
    },
)
def ingest_vacancy(
    payload: dict,
    _user_id: str = Depends(_resolve_user_id),  # noqa: B008
    service: SourceService = Depends(get_source_service),  # noqa: B008
) -> dict:
    """Ingest a vacancy from an external source.

    Expects a JSON body with ``source`` (str) and ``raw_data`` (dict) keys.
    """
    source = payload.get("source")
    raw_data = payload.get("raw_data")

    if not source or not isinstance(source, str):
        raise _http_error(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "validation_error",
            "source is required and must be a string",
        )
    if not raw_data or not isinstance(raw_data, dict):
        raise _http_error(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "validation_error",
            "raw_data is required and must be a dict",
        )

    try:
        vacancy = service.ingest_vacancy(source, raw_data)
    except NotImplementedError as exc:
        raise _http_error(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "unsupported_source",
            str(exc),
        ) from exc

    return {
        "id": str(vacancy.id),
        "source": vacancy.source,
        "source_id": vacancy.source_id,
        "title": vacancy.title,
    }


__all__ = [
    "get_source_service",
    "router",
]
