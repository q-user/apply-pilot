"""Shared bearer-token authentication for the admin slices (issue #145).

The M6/M8 admin surface (``/admin/integrations``, ``/admin/health``,
``/admin/scoring/*``, ``/admin/scoring-review/*``, ``/admin/sources/metrics``)
was historically unauthenticated. This module introduces a single
``require_admin_user`` dependency that all admin routes share.

The dependency is gated behind the ``APP_ADMIN_REQUIRE_AUTH`` env flag
(``True`` by default — see :func:`apply_pilot.config.get_admin_auth_required`).
Operators that need to roll out the change gradually can flip the flag
to ``False`` to restore the pre-fix behaviour for a given deployment.

When the flag is on, a missing or invalid bearer token yields a 401
response with the same JSON shape used by the ``/cover-letter-style``
router (``{"code": ..., "message": ...}``).
"""

from __future__ import annotations

import logging

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from apply_pilot.config import get_admin_auth_required
from apply_pilot.features.users.security import InvalidTokenError, default_token_store

_LOGGER = logging.getLogger("apply_pilot.features.admin.auth")

# ``auto_error=False`` lets us return our own 401 with a stable JSON
# shape instead of FastAPI's default ``{"detail": "Not authenticated"}``.
_bearer_scheme = HTTPBearer(auto_error=False)


def _unauthorised(code: str, message: str) -> HTTPException:
    """Return a JSON-shaped 401 the admin auth contract promises."""
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail={"code": code, "message": message},
    )


def require_admin_user(
    auth_required: bool = Depends(get_admin_auth_required),  # noqa: B008
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer_scheme),  # noqa: B008
) -> str:
    """Validate the caller and return the resolved user id.

    Behaviour is controlled by the :func:`get_admin_auth_required` setting:

    * ``True`` (the default) — the request must carry a valid bearer
      token issued by :func:`apply_pilot.features.users.security.issue_token`.
      A missing token returns ``401 authentication_required``; an unknown
      or expired token returns ``401 invalid_token``.
    * ``False`` — the dependency is a no-op and returns the literal
      string ``"anonymous"``. Use only for local development or behind
      a network ACL that already restricts access to the admin surface.

    Tests override the underlying :func:`get_admin_auth_required`
    dependency through :attr:`fastapi.FastAPI.dependency_overrides` to
    flip the flag without touching the environment.
    """
    if not auth_required:
        return "anonymous"
    if credentials is None:
        raise _unauthorised("authentication_required", "bearer token is required")
    tokens = default_token_store()
    try:
        return tokens.resolve(credentials.credentials)
    except InvalidTokenError as exc:
        _LOGGER.info(
            "admin.auth.invalid_token",
            extra={"event": "admin.auth.invalid_token"},
        )
        raise _unauthorised("invalid_token", "the supplied token is invalid or expired") from exc


__all__ = ["require_admin_user"]
