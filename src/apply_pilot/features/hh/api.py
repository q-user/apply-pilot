"""FastAPI router for the HH credentials slice.

Endpoints
---------

* ``POST /hh/credentials`` — store encrypted credentials (authenticated).
* ``GET /hh/credentials`` — check if credentials exist (metadata only).
* ``DELETE /hh/credentials`` — remove stored credentials.
* ``GET /hh/oauth/authorize`` — start the OAuth2 flow (authenticated).
* ``GET /hh/oauth/callback`` — OAuth2 callback (public, no auth).
* ``POST /hh/oauth/refresh`` — refresh the access token (authenticated).
* ``POST /hh/resumes/sync`` — pull resume metadata from hh.ru (authenticated).
* ``GET /hh/resumes`` — list the caller's stored hh resume links.
"""

from __future__ import annotations

import logging
import threading
import uuid
from collections.abc import Callable

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from apply_pilot.config import HhOAuthSettings, get_hh_oauth_settings
from apply_pilot.db import get_db
from apply_pilot.features.hh.encryption import CredentialEncryptor
from apply_pilot.features.hh.oauth import (
    HhAuthService,
    HhHttpOAuthClient,
    HhOAuthClient,
    HhOAuthStateStore,
    InvalidOAuthStateError,
    MissingRefreshTokenError,
    OAuthExchangeError,
)
from apply_pilot.features.hh.repository import SqlHHCredentialRepository
from apply_pilot.features.hh.resumes import (
    HhHttpResumesClient,
    HhResumesError,
    HhResumesSyncService,
    SqlHhResumeLinkRepository,
)
from apply_pilot.features.hh.schemas import (
    CredentialCheck,
    CredentialsStoreRequest,
    HhResumeLinkDTO,
    HhResumesListResponse,
    HhResumesSyncResponse,
)
from apply_pilot.features.hh.service import HHCredentialService
from apply_pilot.features.users.security import InvalidTokenError
from apply_pilot.shared.errors import NotFoundError

_LOGGER = logging.getLogger("apply_pilot.features.hh.api")

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
    from apply_pilot.features.users.api import get_auth_service

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


# ---------------------------------------------------------------------------
# OAuth dependencies and route handlers
# ---------------------------------------------------------------------------


# Process-wide default instances. The state store is the same shape as
# the telegram linking service: an in-process dict. The HTTP client is
# constructed lazily on first use so importing this module never
# blocks waiting on the config.
_default_state_store: HhOAuthStateStore = HhOAuthStateStore()
_default_oauth_client: HhHttpOAuthClient | None = None
_default_oauth_client_lock = threading.Lock()


def get_hh_oauth_settings_dep() -> HhOAuthSettings:
    """Build :class:`HhOAuthSettings` for the current request.

    Production wiring (and the test suite) can override this dependency
    to inject custom values. The dependency raises ``ValueError`` at
    start-up if the env vars are missing, which propagates through
    FastAPI as a 500.
    """
    return get_hh_oauth_settings()


def get_hh_oauth_state_store() -> HhOAuthStateStore:
    """Return the process-wide :class:`HhOAuthStateStore`."""
    return _default_state_store


def get_hh_oauth_client(
    settings: HhOAuthSettings = Depends(get_hh_oauth_settings_dep),  # noqa: B008
) -> HhOAuthClient:
    """Build (or return the cached) production :class:`HhHttpOAuthClient`.

    A single client is reused across requests so we do not pay the
    connection-pool setup cost on every authorize/refresh call. The
    lock guards the lazy initialisation only.
    """
    global _default_oauth_client
    if _default_oauth_client is not None:
        return _default_oauth_client
    with _default_oauth_client_lock:
        if _default_oauth_client is None:
            _default_oauth_client = HhHttpOAuthClient(
                client_id=settings.client_id,
                client_secret=settings.client_secret,
                redirect_uri=settings.redirect_uri,
            )
    return _default_oauth_client


def get_hh_auth_service(
    *,
    settings: HhOAuthSettings = Depends(get_hh_oauth_settings_dep),  # noqa: B008
    state_store: HhOAuthStateStore = Depends(get_hh_oauth_state_store),  # noqa: B008
    oauth_client: HhOAuthClient = Depends(get_hh_oauth_client),  # noqa: B008
    session: Session = Depends(get_db),  # noqa: B008
    encryptor: CredentialEncryptor = Depends(_get_encryptor),  # noqa: B008
) -> HhAuthService:
    """Build an :class:`HhAuthService` for the current request."""
    repo = SqlHHCredentialRepository(session=session)
    credential_service = HHCredentialService(repo=repo, encryptor=encryptor)
    return HhAuthService(
        oauth_client=oauth_client,
        state_store=state_store,
        credential_service=credential_service,
        client_id=settings.client_id,
        redirect_uri=settings.redirect_uri,
    )


@router.get(
    "/oauth/authorize",
    responses={
        401: {"description": "Missing or invalid bearer token"},
    },
)
def authorize(
    service: HhAuthService = Depends(get_hh_auth_service),  # noqa: B008
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer_scheme),  # noqa: B008
    session: Session = Depends(get_db),  # noqa: B008
) -> dict[str, str]:
    """Start the OAuth2 authorization-code flow.

    Returns a JSON body with ``authorization_url`` (where the user
    should be redirected) and ``state`` (the CSRF token echoed back
    by the callback). Clients that prefer a 302 redirect can issue
    one themselves; this endpoint stays JSON so it is easy to consume
    from a non-browser context.
    """
    user_id_str = _resolve_user_id(credentials, session)
    return service.start_authorization(user_id=uuid.UUID(user_id_str))


@router.get(
    "/oauth/callback",
    responses={
        400: {"description": "Invalid or expired state"},
        502: {"description": "hh.ru OAuth server returned an error"},
    },
)
async def oauth_callback(
    code: str = Query(..., min_length=1),  # noqa: B008
    state: str = Query(..., min_length=1),  # noqa: B008
    service: HhAuthService = Depends(get_hh_auth_service),  # noqa: B008
) -> dict:
    """OAuth2 callback handler.

    Validates the state, exchanges the authorization code for tokens,
    and persists the result via :class:`HHCredentialService`. The
    endpoint is intentionally public — hh.ru redirects the user's
    browser here, with no bearer token to present.

    Returns the same redacted metadata as ``POST /hh/credentials``.
    Clients may choose to 302 the user to a frontend page; this
    endpoint stays JSON so an API client can drive the flow
    programmatically.
    """
    try:
        return await service.handle_callback(code=code, state=state)
    except InvalidOAuthStateError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"code": exc.code, "message": exc.message},
        ) from exc
    except OAuthExchangeError as exc:
        _LOGGER.warning("hh.oauth.exchange_failed", extra={"status_code": exc.status_code})
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail={"code": "oauth_exchange_failed", "message": str(exc)},
        ) from exc


@router.post(
    "/oauth/refresh",
    responses={
        400: {"description": "Stored credentials lack a refresh token"},
        401: {"description": "Missing or invalid bearer token"},
        404: {"description": "No stored credentials for this user"},
        502: {"description": "hh.ru OAuth server returned an error"},
    },
)
async def oauth_refresh(
    service: HhAuthService = Depends(get_hh_auth_service),  # noqa: B008
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer_scheme),  # noqa: B008
    session: Session = Depends(get_db),  # noqa: B008
) -> dict:
    """Refresh the hh.ru access token for the authenticated user.

    Reads the stored refresh token, exchanges it for a fresh access
    token (and, typically, a new refresh token), and updates the
    stored credentials.
    """
    user_id_str = _resolve_user_id(credentials, session)
    try:
        return await service.refresh_user_token(user_id=uuid.UUID(user_id_str))
    except NotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": exc.code, "message": exc.message},
        ) from exc
    except MissingRefreshTokenError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"code": exc.code, "message": exc.message},
        ) from exc
    except OAuthExchangeError as exc:
        _LOGGER.warning("hh.oauth.refresh_failed", extra={"status_code": exc.status_code})
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail={"code": "oauth_exchange_failed", "message": str(exc)},
        ) from exc


# ---------------------------------------------------------------------------
# Resume metadata sync (issue #21)
# ---------------------------------------------------------------------------


# Process-wide default HTTP client for the resumes slice. A single
# :class:`httpx.AsyncClient` is reused across requests so the
# connection-pool setup cost is paid once per process. The instance is
# built lazily on the first request so importing this module never
# blocks waiting on the network layer.
_default_resumes_http: httpx.AsyncClient | None = None
_default_resumes_http_lock = threading.Lock()


def get_hh_resumes_http_client() -> httpx.AsyncClient:
    """Return the process-wide :class:`httpx.AsyncClient` for the resumes slice.

    Tests can override the dependency to inject a client backed by
    :class:`httpx.MockTransport`. Production callers receive a lazily-
    initialised shared client; tests that want isolation create their
    own :class:`HhHttpResumesClient` directly.
    """
    global _default_resumes_http
    if _default_resumes_http is not None:
        return _default_resumes_http
    with _default_resumes_http_lock:
        if _default_resumes_http is None:
            _default_resumes_http = httpx.AsyncClient(timeout=30.0)
    return _default_resumes_http


def _build_resumes_token_provider(
    *,
    credential_service: HHCredentialService,
    user_id: uuid.UUID,
) -> Callable[[], str | None]:
    """Return a closure that mints a fresh bearer token per request.

    The closure calls the credential service on every invocation so a
    refreshed access token (e.g. after ``POST /hh/oauth/refresh``) is
    picked up without rebuilding the HTTP client.
    """

    def _provider() -> str | None:
        try:
            creds = credential_service.get_credentials(user_id)
        except NotFoundError:
            return None
        return creds.access_token

    return _provider


def get_hh_resumes_sync_service(
    session: Session = Depends(get_db),  # noqa: B008
    http_client: httpx.AsyncClient = Depends(get_hh_resumes_http_client),  # noqa: B008
    encryptor: CredentialEncryptor = Depends(_get_encryptor),  # noqa: B008
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer_scheme),  # noqa: B008
) -> HhResumesSyncService:
    """Build an :class:`HhResumesSyncService` for the current request.

    The service binds the caller's ``user_id`` (resolved from the
    bearer token) and a per-user ``token_provider`` closure so the
    service can talk to hh.ru without knowing how the credential
    service works internally.
    """
    user_id_str = _resolve_user_id(credentials, session)
    user_id = uuid.UUID(user_id_str)
    credential_service = HHCredentialService(
        repo=SqlHHCredentialRepository(session=session), encryptor=encryptor
    )
    resumes_client = HhHttpResumesClient(
        client=http_client,
        token_provider=_build_resumes_token_provider(
            credential_service=credential_service, user_id=user_id
        ),
    )
    link_repo = SqlHhResumeLinkRepository(session=session)
    return HhResumesSyncService(
        resumes_client=resumes_client,
        credential_service=credential_service,
        link_repo=link_repo,
        user_id=user_id,
    )


@router.post(
    "/resumes/sync",
    response_model=HhResumesSyncResponse,
    responses={
        401: {"description": "Missing or invalid bearer token"},
        404: {"description": "No hh.ru credentials stored for the user"},
        502: {"description": "hh.ru returned an error response"},
    },
)
async def sync_resumes(
    service: HhResumesSyncService = Depends(get_hh_resumes_sync_service),  # noqa: B008
) -> HhResumesSyncResponse:
    """Pull the caller's resume metadata from hh.ru and persist the links.

    The endpoint refreshes the stored access token implicitly via the
    ``token_provider`` closure on every call, so a recent
    ``POST /hh/oauth/refresh`` is picked up automatically. If the user
    has not linked their hh.ru account yet, the call returns 404.
    """
    try:
        links = await service.sync_metadata()
    except NotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": exc.code, "message": exc.message},
        ) from exc
    except HhResumesError as exc:
        _LOGGER.warning("hh.resumes.sync_failed", extra={"code": exc.code})
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail={"code": exc.code, "message": exc.message},
        ) from exc

    return HhResumesSyncResponse(
        items=[HhResumeLinkDTO.model_validate(link) for link in links],
        synced_count=len(links),
    )


@router.get(
    "/resumes",
    response_model=HhResumesListResponse,
    responses={
        401: {"description": "Missing or invalid bearer token"},
    },
)
def list_resumes(
    session: Session = Depends(get_db),  # noqa: B008
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer_scheme),  # noqa: B008
) -> HhResumesListResponse:
    """Return the caller's stored hh resume links, oldest-first."""
    user_id_str = _resolve_user_id(credentials, session)
    user_id = uuid.UUID(user_id_str)
    repo = SqlHhResumeLinkRepository(session=session)
    links = repo.list_by_user(user_id)
    return HhResumesListResponse(items=[HhResumeLinkDTO.model_validate(link) for link in links])


__all__ = [
    "get_hh_auth_service",
    "get_hh_oauth_client",
    "get_hh_oauth_state_store",
    "get_hh_oauth_settings_dep",
    "get_hh_resumes_http_client",
    "get_hh_resumes_sync_service",
    "get_hh_service",
    "router",
]
