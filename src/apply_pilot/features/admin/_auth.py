"""Shared bearer-token + cookie authentication for the admin slices.

The M6/M8 admin surface (``/admin/integrations``, ``/admin/health``,
``/admin/scoring/*``, ``/admin/scoring-review/*``, ``/admin/sources/metrics``)
is gated behind a single :func:`require_admin_user` dependency that
every admin route shares. The dependency is also the source of truth
for the new ``/admin/`` HTML surface (issue #171).

Two consecutive issues tightened the contract:

* **Issue #145** — added bearer-token auth (the
  ``APP_ADMIN_REQUIRE_AUTH`` env flag, ``True`` by default).
* **Issue #171** — added the ``is_admin`` gate. The dependency now
  resolves the user record (not just the id) and rejects non-admin
  callers with ``403``. A new :func:`resolve_admin_user` dependency
  returns the full :class:`User` row so the HTML landing page can
  show the operator's email in the header.

Both the ``Authorization: Bearer`` header and the session cookie
introduced in PR #170 (issue #169) are accepted; the header wins when
both are present, matching the auth-slice convention. The dependency
is also a drop-in for tests: legacy test suites that don't want to
provision a real ``User`` row can register a dependency override that
returns any string.
"""

from __future__ import annotations

import logging
import uuid

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.orm import Session

from apply_pilot.config import get_admin_auth_required
from apply_pilot.db import get_db
from apply_pilot.features.users.models import User
from apply_pilot.features.users.security import (
    InvalidTokenError,
    TokenStore,
    default_token_store,
)
from apply_pilot.features.users.session import get_session_token

_LOGGER = logging.getLogger("apply_pilot.features.admin.auth")

# ``auto_error=False`` lets us return our own 401 with a stable JSON
# shape instead of FastAPI's default ``{"detail": "Not authenticated"}``.
_bearer_scheme = HTTPBearer(auto_error=False)


def get_token_store() -> TokenStore:
    """Return the :class:`TokenStore` used by the admin auth gate.

    Defaults to the process-wide :func:`default_token_store` so
    production behaviour is identical to the pre-#209 wiring. Tests
    (and any future Redis-backed deployment) override this dependency
    via :attr:`fastapi.FastAPI.dependency_overrides` to inject a custom
    store without monkey-patching :mod:`apply_pilot.features.users.security`.
    """
    return default_token_store()


def _http_error(status_code: int, code: str, message: str) -> HTTPException:
    """Return a JSON-shaped error response that the admin auth contract promises."""
    return HTTPException(status_code=status_code, detail={"code": code, "message": message})


def _resolve_bearer_token(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None,
) -> str | None:
    """Return the bearer token from the Authorization header OR the cookie.

    Mirrors :func:`apply_pilot.features.users.api._resolve_bearer_token`:
    the header takes precedence (bearer is the canonical credential)
    and the session cookie is a fallback so a browser-based client
    can authenticate without any client-side JavaScript.
    """
    if credentials is not None and credentials.credentials:
        return credentials.credentials
    return get_session_token(request)


def _resolve_user_id_from_token(token: str, token_store: TokenStore) -> str:
    """Resolve ``token`` through the injected *token_store*.

    Raises :class:`InvalidTokenError` (caught and translated to 401
    by the caller) for unknown / expired / revoked tokens.
    """
    return token_store.resolve(token)


def _load_admin_flag(session: Session, user_id: uuid.UUID) -> tuple[bool, bool] | None:
    """Return ``(is_active, is_admin)`` for *user_id* or ``None`` if missing.

    Implemented as a column-level query (not a full ORM load) so the
    session is not burdened with a User instance that would later be
    detached when FastAPI's ``get_db`` dependency closes the session.
    """
    statement = select(User.is_active, User.is_admin).where(User.id == user_id)
    row = session.execute(statement).one_or_none()
    if row is None:
        return None
    return bool(row[0]), bool(row[1])


def require_admin_user(
    request: Request,
    auth_required: bool = Depends(get_admin_auth_required),  # noqa: B008
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer_scheme),  # noqa: B008
    session: Session = Depends(get_db),  # noqa: B008
    token_store: TokenStore = Depends(get_token_store),  # noqa: B008
) -> str:
    """Validate the caller and return the resolved user id (as a string).

    Behaviour is controlled by the :func:`get_admin_auth_required` setting:

    * ``True`` (the default) — the request must carry a valid bearer
      token (header or session cookie) issued by
      :func:`apply_pilot.features.users.security.issue_token`. The
      resolved user must also have ``is_admin=True`` (issue #171).

      - Missing credential → ``401 authentication_required``.
      - Unknown / expired token → ``401 invalid_token``.
      - Token resolves but user is missing / inactive → ``401 invalid_token``.
      - Token resolves and the user is not an admin → ``403 admin_required``.

    * ``False`` — the dependency is a no-op and returns the literal
      string ``"anonymous"``. Use only for local development or behind
      a network ACL that already restricts access to the admin surface.

    The :class:`TokenStore` is supplied through FastAPI's dependency
    injection (issue #209). Production wiring keeps the default
    :func:`default_token_store`; tests inject a fresh store via
    :attr:`fastapi.FastAPI.dependency_overrides` keyed on
    :func:`get_token_store`. The pre-issue-#171 test suites
    (``test_admin_api``, ``test_integrations``) further override
    :func:`require_admin_user` itself: they issue tokens for a
    synthetic UUID that has no backing ``User`` row, so the strict
    lookup would 401/403. The override is documented in those files.
    """
    if not auth_required:
        return "anonymous"
    token = _resolve_bearer_token(request, credentials)
    if token is None:
        raise _http_error(
            status.HTTP_401_UNAUTHORIZED,
            "authentication_required",
            "bearer token or session cookie is required",
        )
    try:
        user_id_str = _resolve_user_id_from_token(token, token_store)
    except InvalidTokenError as exc:
        _LOGGER.info(
            "admin.auth.invalid_token",
            extra={"event": "admin.auth.invalid_token"},
        )
        raise _http_error(
            status.HTTP_401_UNAUTHORIZED,
            "invalid_token",
            "the supplied token is invalid or expired",
        ) from exc

    try:
        user_uuid = uuid.UUID(user_id_str)
    except (TypeError, ValueError) as exc:
        raise _http_error(
            status.HTTP_401_UNAUTHORIZED,
            "invalid_token",
            "the token does not reference a valid user id",
        ) from exc

    flags = _load_admin_flag(session, user_uuid)
    if flags is None:
        raise _http_error(
            status.HTTP_401_UNAUTHORIZED,
            "invalid_token",
            "the user behind the token no longer exists",
        )
    is_active, is_admin = flags
    if not is_active:
        raise _http_error(
            status.HTTP_401_UNAUTHORIZED,
            "invalid_token",
            "the user behind the token is inactive",
        )
    if not is_admin:
        _LOGGER.info(
            "admin.auth.not_admin",
            extra={"event": "admin.auth.not_admin", "user_id": user_id_str},
        )
        raise _http_error(
            status.HTTP_403_FORBIDDEN,
            "admin_required",
            "this endpoint requires an admin user",
        )
    return user_id_str


def resolve_admin_user(
    request: Request,
    auth_required: bool = Depends(get_admin_auth_required),  # noqa: B008
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer_scheme),  # noqa: B008
    session: Session = Depends(get_db),  # noqa: B008
    token_store: TokenStore = Depends(get_token_store),  # noqa: B008
) -> User:
    """Validate the caller and return the resolved :class:`User` record.

    A companion to :func:`require_admin_user` that returns the full
    :class:`User` ORM row instead of the user id. Used by the new
    admin HTML surface (issue #171) so the landing page can show the
    operator's email and so route handlers can read ``user.email``
    without doing the lookup twice.

    The auth check is identical to :func:`require_admin_user` — same
    401 / 403 contract, same header/cookie fallback, same production
    gate, same pluggable :class:`TokenStore` (issue #209). The two
    functions could be merged, but keeping them apart avoids touching
    every ``_admin_user: str = Depends(require_admin_user)`` consumer
    in the JSON admin endpoints.
    """
    if not auth_required:
        raise _http_error(
            status.HTTP_401_UNAUTHORIZED,
            "authentication_required",
            "admin endpoints require a session in production",
        )
    token = _resolve_bearer_token(request, credentials)
    if token is None:
        raise _http_error(
            status.HTTP_401_UNAUTHORIZED,
            "authentication_required",
            "bearer token or session cookie is required",
        )
    try:
        user_id_str = _resolve_user_id_from_token(token, token_store)
    except InvalidTokenError as exc:
        _LOGGER.info(
            "admin.auth.invalid_token",
            extra={"event": "admin.auth.invalid_token"},
        )
        raise _http_error(
            status.HTTP_401_UNAUTHORIZED,
            "invalid_token",
            "the supplied token is invalid or expired",
        ) from exc

    try:
        user_uuid = uuid.UUID(user_id_str)
    except (TypeError, ValueError) as exc:
        raise _http_error(
            status.HTTP_401_UNAUTHORIZED,
            "invalid_token",
            "the token does not reference a valid user id",
        ) from exc

    user = session.get(User, user_uuid)
    if user is None or not user.is_active:
        raise _http_error(
            status.HTTP_401_UNAUTHORIZED,
            "invalid_token",
            "the user behind the token no longer exists or is inactive",
        )
    if not user.is_admin:
        _LOGGER.info(
            "admin.auth.not_admin",
            extra={"event": "admin.auth.not_admin", "user_id": user_id_str},
        )
        raise _http_error(
            status.HTTP_403_FORBIDDEN,
            "admin_required",
            "this endpoint requires an admin user",
        )
    return user


__all__ = ["get_token_store", "require_admin_user", "resolve_admin_user"]
