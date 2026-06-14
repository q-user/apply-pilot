"""FastAPI router for the auth slice.

Endpoints
---------

* ``POST /auth/register`` — create a new user, return the public
  :class:`UserRead` payload. Does NOT log the user in; clients should
  follow up with ``/auth/login``.
* ``POST /auth/login`` — verify credentials, return a bearer token
  plus the user payload.
* ``POST /auth/logout`` — invalidate the bearer token (idempotent).
* ``GET /auth/me`` — return the user behind the bearer token.

Wiring
------

The router declares a :func:`get_auth_service` dependency. Production
wiring builds the service with a SQLAlchemy-backed repository, while
tests inject a fake. A request-scoped :func:`get_token_store` is
exposed for the same reason: tokens live in an in-memory store today
but a Redis-backed implementation can drop in later without touching
the route handlers.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Response, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from job_apply.db import get_db
from job_apply.features.users.repository import SqlAlchemyUsersRepository
from job_apply.features.users.schemas import (
    AuthenticatedUser,
    UserCreate,
    UserLogin,
    UserRead,
)
from job_apply.features.users.security import (
    InvalidTokenError,
    TokenStore,
    default_token_store,
)
from job_apply.features.users.service import (
    AuthenticationError,
    AuthService,
    DuplicateEmailError,
)

_LOGGER = logging.getLogger("job_apply.features.users.api")

router = APIRouter(prefix="/auth", tags=["auth"])

# ``auto_error=False`` lets us return our own 401 with a stable JSON
# shape instead of FastAPI's default ``{"detail": "Not authenticated"}``.
_bearer_scheme = HTTPBearer(auto_error=False)


def get_token_store() -> TokenStore:
    """Default token store used by the router.

    Returns a process-wide :func:`default_token_store` so tokens
    issued by one request remain resolvable by the next. Production
    wiring can override this dependency to plug in a Redis-backed or
    multi-process store.
    """
    return default_token_store()


def get_auth_service(
    session: Session = Depends(get_db),  # noqa: B008
    tokens: TokenStore = Depends(get_token_store),  # noqa: B008
) -> AuthService:
    """Build an :class:`AuthService` for the current request.

    The service owns a single repository backed by the request's
    session. The session itself is closed by the ``get_db`` generator
    once the response is sent.
    """
    repo = SqlAlchemyUsersRepository(session=session)
    return AuthService(users_repo=repo, tokens=tokens)


def _http_error(status_code: int, code: str, message: str) -> HTTPException:
    """Return a JSON-shaped 4xx error that the API contract promises."""
    return HTTPException(status_code=status_code, detail={"code": code, "message": message})


# ---------------------------------------------------------------------------
# Route handlers
# ---------------------------------------------------------------------------


@router.post(
    "/register",
    response_model=UserRead,
    status_code=status.HTTP_201_CREATED,
    responses={
        409: {"description": "Email already registered"},
        422: {"description": "Validation error"},
    },
)
def register(
    payload: UserCreate,
    service: AuthService = Depends(get_auth_service),  # noqa: B008
) -> UserRead:
    """Create a new user account."""
    try:
        return service.register(payload)
    except DuplicateEmailError as exc:
        _LOGGER.info("auth.register.conflict", extra={"email": payload.email})
        raise _http_error(status.HTTP_409_CONFLICT, exc.code, exc.message) from exc


@router.post(
    "/login",
    response_model=AuthenticatedUser,
    responses={
        401: {"description": "Invalid credentials"},
        422: {"description": "Validation error"},
    },
)
def login(
    payload: UserLogin,
    service: AuthService = Depends(get_auth_service),  # noqa: B008
) -> AuthenticatedUser:
    """Verify credentials and return a bearer token + user payload."""
    try:
        return service.login(email=payload.email, password=payload.password)
    except AuthenticationError as exc:
        # 401 is the same code for unknown email, wrong password, or
        # inactive user; the response body never reveals which.
        _LOGGER.info("auth.login.failed", extra={"email": payload.email})
        raise _http_error(
            status.HTTP_401_UNAUTHORIZED, exc.code, "invalid email or password"
        ) from exc


@router.post(
    "/logout",
    status_code=status.HTTP_204_NO_CONTENT,
    responses={
        401: {"description": "Missing or invalid bearer token"},
    },
)
def logout(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer_scheme),  # noqa: B008
    tokens: TokenStore = Depends(get_token_store),  # noqa: B008
) -> Response:
    """Invalidate the supplied bearer token.

    Idempotent: a missing or already-revoked token still returns 204
    when the header is present (and 401 when the header is missing).
    """
    if credentials is None:
        raise _http_error(
            status.HTTP_401_UNAUTHORIZED,
            "authentication_required",
            "bearer token is required",
        )
    tokens.revoke(credentials.credentials)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get(
    "/me",
    response_model=UserRead,
    responses={
        401: {"description": "Missing or invalid bearer token"},
    },
)
def me(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer_scheme),  # noqa: B008
    service: AuthService = Depends(get_auth_service),  # noqa: B008
) -> UserRead:
    """Return the user behind the bearer token."""
    if credentials is None:
        raise _http_error(
            status.HTTP_401_UNAUTHORIZED,
            "authentication_required",
            "bearer token is required",
        )
    try:
        user_id = service.resolve_user_id_from_token(credentials.credentials)
    except InvalidTokenError as exc:
        raise _http_error(status.HTTP_401_UNAUTHORIZED, "invalid_token", str(exc)) from exc
    try:
        return service.get_user(user_id=user_id)
    except AuthenticationError as exc:
        raise _http_error(status.HTTP_401_UNAUTHORIZED, "invalid_token", str(exc)) from exc


__all__ = [
    "get_auth_service",
    "get_token_store",
    "router",
]
