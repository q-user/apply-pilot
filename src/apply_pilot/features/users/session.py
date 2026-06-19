"""Browser-friendly session cookie for the auth slice (M6, issue #169).

The M6 frontend shell is plain HTML forms, not a SPA, so the
``/auth/login`` endpoint has to accept a ``<form method="post">``
submission in addition to the existing JSON contract. This module
isolates the cookie contract — name, attributes, env-driven
``secure`` flag — and exposes three small helpers that the
``users.api`` router composes.

The helpers are intentionally tiny and free of any transport-layer
imports: the cookie is set on a :class:`fastapi.Response` (which the
caller already has via dependency injection) and read from a
:class:`fastapi.Request` (same). No new ORM tables, no new token
shapes — the raw bearer token issued by :mod:`security` IS the cookie
value, and the existing :class:`UserSession` rows already store the
SHA-256 hash that backs :meth:`AuthService.resolve_user_id_from_token`.
"""

from __future__ import annotations

import os
from functools import lru_cache
from typing import Literal

from fastapi import Request, Response
from pydantic_settings import BaseSettings, SettingsConfigDict

#: Default cookie name. Stable, exposed so other slices (admin, dashboard)
#: can read the same cookie without re-declaring the string.
SESSION_COOKIE_NAME: str = "apply_pilot_session"

#: Stable login path. Re-exported so the dashboard / admin slices can
#: build ``<form action>`` attributes without hardcoding the string.
LOGIN_PATH: str = "/auth/login"

#: Stable logout path. Same rationale as :data:`LOGIN_PATH`.
LOGOUT_PATH: str = "/auth/logout"

#: Default session lifetime in seconds. Matches the default used by
#: :class:`apply_pilot.features.users.service.AuthService` (8 hours) so
#: the cookie expires at the same time as the in-memory token.
DEFAULT_MAX_AGE_SECONDS: int = 60 * 60 * 8


class AuthSessionSettings(BaseSettings):
    """Configuration for the browser session cookie.

    Settings live in the auth slice (not :mod:`apply_pilot.config`)
    to keep the slice self-contained. The cookie is ``secure`` by
    default — production deploys always run behind TLS — but
    ``APP_ENV=development`` flips it to ``False`` so ``docker compose
    up`` works over plain HTTP.

    Attributes
    ----------
    cookie_name:
        The ``Set-Cookie`` name. Defaults to
        :data:`SESSION_COOKIE_NAME`.
    max_age_seconds:
        How long the browser keeps the cookie. Matches
        :class:`AuthService`'s default token TTL so a cookie cannot
        outlive its underlying bearer token.
    secure:
        ``True`` by default. Set to ``False`` when ``APP_ENV`` is
        ``development`` so local HTTP works.
    httponly:
        ``True`` by default — the bearer token is never reachable
        from JavaScript, mitigating XSS-driven token theft.
    samesite:
        ``"lax"`` by default. ``"lax"`` lets the browser carry the
        cookie on top-level GET navigations (which is what the
        redirect after ``POST /auth/login`` looks like) while
        blocking it on cross-site POSTs.
    """

    model_config = SettingsConfigDict(
        env_prefix="APP_AUTH_SESSION_",
        env_file=None,
        case_sensitive=False,
        extra="ignore",
        frozen=True,
    )

    cookie_name: str = SESSION_COOKIE_NAME
    max_age_seconds: int = DEFAULT_MAX_AGE_SECONDS
    secure: bool = True
    httponly: bool = True
    samesite: Literal["lax", "strict", "none"] = "lax"


def _resolve_secure_flag(default: bool) -> bool:
    """Return the cookie's ``secure`` flag based on ``APP_ENV``.

    The check is intentionally narrow: only the literal ``"development"``
    flips the flag off. Other values (including unset, ``""``, or
    ``"production"``) keep the default — the secure-by-default posture
    is the safest behaviour when the env is misconfigured.
    """
    if os.getenv("APP_ENV", "").strip().lower() == "development":
        return False
    return default


@lru_cache(maxsize=1)
def get_auth_session_settings() -> AuthSessionSettings:
    """Return a cached :class:`AuthSessionSettings` instance.

    Cached so repeated calls in a request lifecycle do not re-read the
    environment. The cache is cleared by tests that need to flip
    ``APP_ENV`` mid-run; see ``test_auth_session.py``.
    """
    base = AuthSessionSettings()
    return base.model_copy(update={"secure": _resolve_secure_flag(base.secure)})


def set_session_cookie(
    response: Response,
    *,
    token: str,
    settings: AuthSessionSettings | None = None,
) -> None:
    """Attach the session cookie to *response*.

    The cookie value is the raw bearer token — the same string the
    :class:`AuthService` issues. The existing :class:`UserSession`
    rows already store its SHA-256 hash, so the cookie transparently
    survives a process restart as long as the SQLAlchemy session
    repository is wired in.
    """
    resolved = settings or get_auth_session_settings()
    response.set_cookie(
        key=resolved.cookie_name,
        value=token,
        max_age=resolved.max_age_seconds,
        path="/",
        secure=resolved.secure,
        httponly=resolved.httponly,
        samesite=resolved.samesite,
    )


def clear_session_cookie(
    response: Response,
    *,
    settings: AuthSessionSettings | None = None,
) -> None:
    """Remove the session cookie from *response*.

    FastAPI's :meth:`Response.delete_cookie` emits a
    ``Set-Cookie: name=; Max-Age=0`` header, which is what every
    browser interprets as "drop this cookie now".
    """
    resolved = settings or get_auth_session_settings()
    response.delete_cookie(key=resolved.cookie_name, path="/")


def get_session_token(
    request: Request,
    *,
    settings: AuthSessionSettings | None = None,
) -> str | None:
    """Return the raw bearer token from the session cookie, or ``None``.

    Returns ``None`` when the cookie is missing — the caller is
    expected to fall back to the ``Authorization: Bearer`` header
    (or 401 when neither is present). The raw token is what
    :meth:`AuthService.resolve_user_id_from_token` consumes, so the
    resolution path is identical to the bearer-header path.
    """
    resolved = settings or get_auth_session_settings()
    return request.cookies.get(resolved.cookie_name)


__all__ = [
    "AuthSessionSettings",
    "DEFAULT_MAX_AGE_SECONDS",
    "LOGIN_PATH",
    "LOGOUT_PATH",
    "SESSION_COOKIE_NAME",
    "clear_session_cookie",
    "get_auth_session_settings",
    "get_session_token",
    "set_session_cookie",
]
