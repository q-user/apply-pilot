"""FastAPI router for the HH credentials slice.

Endpoints
---------

* ``POST /hh/credentials`` — store encrypted credentials (authenticated).
* ``GET /hh/credentials`` — check if credentials exist (metadata only).
* ``DELETE /hh/credentials`` — remove stored credentials.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Response, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from job_apply.db import get_db
from job_apply.features.hh.encryption import CredentialEncryptor
from job_apply.features.hh.repository import SqlHHCredentialRepository
from job_apply.features.hh.schemas import CredentialCheck, CredentialsStoreRequest
from job_apply.features.hh.service import HHCredentialService
from job_apply.features.users.security import InvalidTokenError

router = APIRouter(prefix="/hh", tags=["hh"])

_bearer_scheme = HTTPBearer(auto_error=False)


def _get_encryptor() -> CredentialEncryptor:
    """Production encryptor — reads APP_HH_ENCRYPTION_KEY from the environment.

    In tests, the dependency is overridden at the ``app`` level.
    """
    return CredentialEncryptor.from_env()


def get_hh_service(
    session: Session = Depends(get_db),  # noqa: B008
    encryptor: CredentialEncryptor = Depends(_get_encryptor),  # noqa: B008
) -> HHCredentialService:
    """Build an :class:`HHCredentialService` for the current request."""
    repo = SqlHHCredentialRepository(session=session)
    return HHCredentialService(repo=repo, encryptor=encryptor)


def _resolve_user_id(
    credentials: HTTPAuthorizationCredentials | None,
    session: Session,
) -> str:
    """Resolve a bearer token to a user id, raising 401 on failure."""
    if credentials is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"code": "authentication_required", "message": "bearer token is required"},
        )
    from job_apply.features.users.api import get_auth_service

    auth = get_auth_service(session)
    try:
        user_id = auth.resolve_user_id_from_token(credentials.credentials)
    except InvalidTokenError as err:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"code": "invalid_token", "message": "invalid or expired bearer token"},
        ) from err
    return str(user_id)


# ---------------------------------------------------------------------------
# Route handlers
# ---------------------------------------------------------------------------


@router.post(
    "/credentials",
    status_code=status.HTTP_201_CREATED,
    responses={
        401: {"description": "Missing or invalid bearer token"},
        422: {"description": "Validation error"},
    },
)
def store_credentials(
    payload: CredentialsStoreRequest,
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer_scheme),  # noqa: B008
    session: Session = Depends(get_db),  # noqa: B008
) -> dict:
    """Store (or overwrite) hh.ru OAuth credentials for the authenticated user."""
    user_id_str = _resolve_user_id(credentials, session)

    encryptor = _get_encryptor()
    repo = SqlHHCredentialRepository(session=session)
    service = HHCredentialService(repo=repo, encryptor=encryptor)

    import uuid as _uuid

    result = service.store_credentials(
        user_id=_uuid.UUID(user_id_str),
        access_token=payload.access_token,
        refresh_token=payload.refresh_token,
        token_type=payload.token_type,
        expires_at=payload.expires_at,
    )
    return result.model_dump(mode="json")


@router.get(
    "/credentials",
    response_model=CredentialCheck,
    responses={
        401: {"description": "Missing or invalid bearer token"},
    },
)
def check_credentials(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer_scheme),  # noqa: B008
    session: Session = Depends(get_db),  # noqa: B008
) -> CredentialCheck:
    """Check whether the authenticated user has stored HH credentials.

    Returns metadata only (token_type, expires_at) — never the raw tokens.
    """
    user_id_str = _resolve_user_id(credentials, session)
    import uuid as _uuid

    encryptor = _get_encryptor()
    repo = SqlHHCredentialRepository(session=session)
    service = HHCredentialService(repo=repo, encryptor=encryptor)

    return service.check_credentials(_uuid.UUID(user_id_str))


@router.delete(
    "/credentials",
    status_code=status.HTTP_204_NO_CONTENT,
    responses={
        401: {"description": "Missing or invalid bearer token"},
    },
)
def delete_credentials(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer_scheme),  # noqa: B008
    session: Session = Depends(get_db),  # noqa: B008
) -> Response:
    """Delete stored HH credentials for the authenticated user.

    Idempotent — returns 204 even if no credentials existed.
    """
    user_id_str = _resolve_user_id(credentials, session)
    import uuid as _uuid

    encryptor = _get_encryptor()
    repo = SqlHHCredentialRepository(session=session)
    service = HHCredentialService(repo=repo, encryptor=encryptor)

    service.delete_credentials(_uuid.UUID(user_id_str))
    return Response(status_code=status.HTTP_204_NO_CONTENT)


__all__ = ["get_hh_service", "router"]
