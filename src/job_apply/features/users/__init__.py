"""Users / auth vertical slice.

Public surface
--------------

The slice exposes a single ORM model (:class:`User`) and the
:class:`AuthService` entry point. Other M1 slices import the model
from here so the email/password contract stays in one place.

Endpoints
---------

* ``POST /auth/register`` — create a new user.
* ``POST /auth/login`` — verify credentials, return a bearer token.
* ``POST /auth/logout`` — invalidate a bearer token.
* ``GET /auth/me`` — return the user behind a bearer token.

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
"""

from __future__ import annotations

from job_apply.features.users.models import User
from job_apply.features.users.repository import (
    InMemoryUsersRepository,
    SqlAlchemyUsersRepository,
    UsersRepository,
)
from job_apply.features.users.schemas import (
    AuthenticatedUser,
    AuthToken,
    UserCreate,
    UserLogin,
    UserRead,
)
from job_apply.features.users.security import (
    InMemoryTokenStore,
    InvalidTokenError,
    TokenStore,
    hash_password,
    issue_token,
    verify_password,
    verify_token,
)
from job_apply.features.users.service import (
    AuthenticationError,
    AuthService,
    DuplicateEmailError,
)

__all__ = [
    "AuthService",
    "AuthToken",
    "AuthenticatedUser",
    "AuthenticationError",
    "DuplicateEmailError",
    "InMemoryTokenStore",
    "InMemoryUsersRepository",
    "InvalidTokenError",
    "SqlAlchemyUsersRepository",
    "TokenStore",
    "User",
    "UserCreate",
    "UserLogin",
    "UserRead",
    "UsersRepository",
    "hash_password",
    "issue_token",
    "verify_password",
    "verify_token",
]
