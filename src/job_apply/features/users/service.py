"""Auth use-case service.

The service is the only place where ORM rows, password hashes, and
bearer tokens are combined into user-facing operations. It raises
``DomainError`` subclasses (or the slice-local exceptions declared
here) and lets the FastAPI layer translate those to HTTP responses.

``AuthService`` accepts its collaborators by constructor injection:

* ``users_repo`` — a :class:`UsersRepository` (in-memory fake or
  SQLAlchemy production implementation).
* ``tokens`` — a :class:`TokenStore` (in-memory for now; a Redis-backed
  implementation can replace it later without touching this class).
* ``token_ttl_seconds`` — how long an issued token stays valid.
* ``clock`` — a callable returning the current time as a float; tests
  may inject a stub to make expiry deterministic.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable

from job_apply.features.users.models import User
from job_apply.features.users.repository import UsersRepository
from job_apply.features.users.schemas import AuthenticatedUser, UserCreate, UserRead
from job_apply.features.users.security import (
    InMemoryTokenStore,
    InvalidTokenError,
    TokenStore,
    hash_password,
    verify_password,
)
from job_apply.shared.errors import ConflictError


class DuplicateEmailError(ConflictError):
    """A registration collided with an existing user (same email)."""

    code: str = "duplicate_email"


class AuthenticationError(Exception):
    """Credentials are wrong, the user is inactive, or the user is missing.

    Kept as a plain :class:`Exception` (not a :class:`DomainError`) so
    the HTTP layer always returns 401 regardless of how
    :class:`job_apply.shared.errors.DomainError` evolves. The slice
    never returns different status codes for the two failure modes
    (unknown email vs wrong password) on purpose: the error message is
    the same to avoid leaking account existence to attackers.
    """

    code: str = "authentication_failed"


def _user_to_dto(user: User) -> UserRead:
    """Map an ORM ``User`` row to a public :class:`UserRead` DTO."""
    return UserRead(
        id=user.id,
        email=user.email,
        is_active=user.is_active,
        created_at=user.created_at,
        updated_at=user.updated_at,
    )


class AuthService:
    """Registration, login, current-user lookup, and logout."""

    def __init__(
        self,
        *,
        users_repo: UsersRepository,
        tokens: TokenStore | None = None,
        token_ttl_seconds: int = 60 * 60 * 8,  # 8 hours
        clock: Callable[[], float] | None = None,
    ) -> None:
        self._users_repo = users_repo
        self._tokens = tokens or InMemoryTokenStore()
        self._token_ttl_seconds = token_ttl_seconds
        # Clock is currently unused; kept on the constructor so a
        # later deterministic-time test can drop one in without an
        # API change. (security.py uses ``time.monotonic`` directly.)
        self._clock = clock

    # ------------------------------------------------------------------
    # Public contract
    # ------------------------------------------------------------------

    @property
    def users_repo(self) -> UsersRepository:
        """Expose the repository for tests that need to assert state.

        Production code should never reach into this; use the service
        methods instead.
        """
        return self._users_repo

    def register(self, payload: UserCreate) -> UserRead:
        """Create a new user, raising :class:`DuplicateEmailError` on collision."""
        existing = self._users_repo.get_by_email(payload.email)
        if existing is not None:
            raise DuplicateEmailError(f"user with email {payload.email!r} already exists")
        user = self._users_repo.create(
            email=payload.email,
            hashed_password=hash_password(payload.password),
            is_active=True,
        )
        return _user_to_dto(user)

    def login(self, *, email: str, password: str) -> AuthenticatedUser:
        """Verify credentials and return ``(access_token, user)``.

        Raises :class:`AuthenticationError` if the email is unknown, the
        password is wrong, or the user has been deactivated. The error
        message is intentionally identical for all three cases.
        """
        user = self._users_repo.get_by_email(email)
        if user is None or not user.is_active:
            raise AuthenticationError("invalid email or password")
        if not verify_password(password, user.hashed_password):
            raise AuthenticationError("invalid email or password")
        token = self._tokens.issue(str(user.id), ttl_seconds=self._token_ttl_seconds)
        return AuthenticatedUser(access_token=token, user=_user_to_dto(user))

    def resolve_user_id_from_token(self, token: str) -> uuid.UUID:
        """Resolve a bearer token to the underlying user id.

        Raises :class:`InvalidTokenError` for unknown / expired tokens.
        """
        user_id_str = self._tokens.resolve(token)
        try:
            return uuid.UUID(user_id_str)
        except (TypeError, ValueError) as exc:
            raise InvalidTokenError("malformed user id in token") from exc

    def get_user(self, *, user_id: uuid.UUID) -> UserRead:
        """Return the public user payload for ``user_id``."""
        user = self._users_repo.get_by_id(user_id)
        if user is None:
            raise AuthenticationError("user no longer exists")
        return _user_to_dto(user)

    def logout(self, token: str) -> None:
        """Invalidate ``token`` so it can no longer authenticate."""
        # ``revoke`` is silent on unknown tokens; that matches the
        # "idempotent logout" expectation a REST client has.
        self._tokens.revoke(token)


__all__ = [
    "AuthService",
    "AuthenticationError",
    "DuplicateEmailError",
]
