"""Users / auth vertical slice.

Public surface
--------------

The slice exposes a single ORM model (:class:`User`) and the
:class:`AuthService` entry point. Other M1 slices import the model
from here so the email/password contract stays in one place.

Endpoints
---------

* ``POST /auth/register`` — create a new user.
* ``POST /auth/login`` — verify credentials, return a bearer token
  *and* set a browser-friendly session cookie. HTML clients are
  redirected to ``/dashboard`` (or the safe ``next`` query);
  JSON clients get the bearer token only.
* ``POST /auth/logout`` — invalidate a bearer token and clear the
  cookie. HTML clients are redirected to ``/``; JSON clients get a
  204.
* ``GET /auth/login`` — render the inline-HTML login form. A
  valid session cookie bounces the visitor to ``?next=...`` or
  ``/dashboard`` instead.
* ``GET /auth/me`` — return the user behind a bearer token or the
  session cookie.

Storage
-------

* :class:`User` — SQLAlchemy 2.x model.
* :class:`InMemoryUsersRepository` — dict-backed fake for tests.
* :class:`SqlAlchemyUsersRepository` — production persistence gateway.

Security
--------

* :func:`hash_password` / :func:`verify_password` — PBKDF2-HMAC-SHA256.
* :class:`InMemoryTokenStore` / :func:`issue_token` / :func:`verify_token` —
  in-process bearer-token bookkeeping.
* :mod:`apply_pilot.features.users.session` — the browser session
  cookie contract (settings + set/clear/get helpers).
"""

from __future__ import annotations

from apply_pilot.features.users.models import User, UserSession
from apply_pilot.features.users.repository import (
    InMemoryUserSessionRepository,
    InMemoryUsersRepository,
    SqlAlchemyUserSessionRepository,
    SqlAlchemyUsersRepository,
    UserSessionRepository,
    UsersRepository,
)
from apply_pilot.features.users.schemas import (
    AuthenticatedUser,
    AuthToken,
    UserCreate,
    UserLogin,
    UserRead,
)
from apply_pilot.features.users.security import (
    InMemoryTokenStore,
    InvalidTokenError,
    TokenStore,
    hash_password,
    issue_token,
    verify_password,
    verify_token,
)
from apply_pilot.features.users.service import (
    AuthenticationError,
    AuthService,
    DuplicateEmailError,
)
from apply_pilot.features.users.session import (
    LOGIN_PATH,
    LOGOUT_PATH,
    SESSION_COOKIE_NAME,
    AuthSessionSettings,
    clear_session_cookie,
    get_auth_session_settings,
    get_session_token,
    set_session_cookie,
)

__all__ = [
    "AuthService",
    "AuthSessionSettings",
    "AuthToken",
    "AuthenticatedUser",
    "AuthenticationError",
    "DuplicateEmailError",
    "InMemoryTokenStore",
    "InMemoryUserSessionRepository",
    "InMemoryUsersRepository",
    "InvalidTokenError",
    "LOGIN_PATH",
    "LOGOUT_PATH",
    "SESSION_COOKIE_NAME",
    "SqlAlchemyUserSessionRepository",
    "SqlAlchemyUsersRepository",
    "TokenStore",
    "User",
    "UserCreate",
    "UserLogin",
    "UserRead",
    "UserSession",
    "UserSessionRepository",
    "UsersRepository",
    "clear_session_cookie",
    "get_auth_session_settings",
    "get_session_token",
    "hash_password",
    "hash_token",
    "issue_token",
    "set_session_cookie",
    "verify_password",
    "verify_token",
]
