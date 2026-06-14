"""Persistence gateway for the auth slice.

Two repository implementations live here:

* :class:`InMemoryUsersRepository` — a dict-backed fake used by unit
  tests and useful for local experimentation. The dict is keyed by both
  user id and normalised email so lookups are O(1) in either direction.
* :class:`SqlAlchemyUsersRepository` — the production implementation
  that talks to a SQLAlchemy ``Session``. ``__call__`` returns a fresh
  session per call so the FastAPI ``get_db`` dependency can keep its
  existing per-request lifecycle.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Protocol

from sqlalchemy import select
from sqlalchemy.orm import Session

from job_apply.features.users.models import User


class UsersRepository(Protocol):
    """Minimal interface the AuthService relies on."""

    def create(self, *, email: str, hashed_password: str, is_active: bool) -> User: ...
    def get_by_id(self, user_id: uuid.UUID) -> User | None: ...
    def get_by_email(self, email: str) -> User | None: ...


# ---------------------------------------------------------------------------
# In-memory implementation
# ---------------------------------------------------------------------------


class InMemoryUsersRepository:
    """Dict-backed repository for tests and local hacking.

    A second index by normalised email keeps lookups fast without
    scanning all users. Resetting the repository is a matter of
    constructing a new instance, which keeps test isolation simple.
    """

    def __init__(self) -> None:
        self._by_id: dict[uuid.UUID, User] = {}
        self._by_email: dict[str, uuid.UUID] = {}

    def create(self, *, email: str, hashed_password: str, is_active: bool) -> User:
        normalised = email.lower()
        if normalised in self._by_email:
            # Mirror SQLAlchemy's IntegrityError contract for the
            # uniqueness constraint on the email column.
            raise _DuplicateEmailError(normalised)
        user = User(
            id=uuid.uuid4(),
            email=normalised,
            hashed_password=hashed_password,
            is_active=is_active,
        )
        # The SQLAlchemy model relies on ``server_default=func.now()``
        # to populate ``created_at``. The in-memory path has no
        # server, so we mirror the contract explicitly.
        user.created_at = datetime.now(UTC)
        self._by_id[user.id] = user
        self._by_email[normalised] = user.id
        return user

    def get_by_id(self, user_id: uuid.UUID) -> User | None:
        return self._by_id.get(user_id)

    def get_by_email(self, email: str) -> User | None:
        user_id = self._by_email.get(email.lower())
        if user_id is None:
            return None
        return self._by_id.get(user_id)


class _DuplicateEmailError(Exception):
    """Internal sentinel raised by the in-memory repo on email collisions."""


# ---------------------------------------------------------------------------
# SQLAlchemy implementation
# ---------------------------------------------------------------------------


class SqlAlchemyUsersRepository:
    """SQLAlchemy-backed repository.

    The repository can be constructed two ways:

    * With a single ``Session`` (caller-managed lifetime). Useful for
      script-style use cases that already hold an open session.
    * With a ``session_factory`` (default). The repository opens a
      short-lived session per operation and closes it before
      returning. This matches how the FastAPI ``get_db`` dependency
      scopes sessions to a single request.
    """

    def __init__(
        self,
        session: Session | None = None,
        *,
        session_factory: Callable[[], Session] | None = None,
    ) -> None:
        if session is not None and session_factory is not None:
            raise ValueError("pass either session or session_factory, not both")
        self._session = session
        self._session_factory = session_factory

    def _scope(self) -> Session:
        if self._session is not None:
            return self._session
        if self._session_factory is None:
            raise RuntimeError("SqlAlchemyUsersRepository is not bound to a session")
        return self._session_factory()

    def create(self, *, email: str, hashed_password: str, is_active: bool) -> User:
        normalised = email.lower()
        session = self._scope()
        try:
            user = User(
                email=normalised,
                hashed_password=hashed_password,
                is_active=is_active,
            )
            session.add(user)
            session.commit()
            session.refresh(user)
            return user
        except Exception:
            session.rollback()
            raise
        finally:
            if self._session is None:
                session.close()

    def get_by_id(self, user_id: uuid.UUID) -> User | None:
        session = self._scope()
        try:
            return session.get(User, user_id)
        finally:
            if self._session is None:
                session.close()

    def get_by_email(self, email: str) -> User | None:
        normalised = email.lower()
        session = self._scope()
        try:
            statement = select(User).where(User.email == normalised)
            return session.execute(statement).scalar_one_or_none()
        finally:
            if self._session is None:
                session.close()


__all__ = [
    "InMemoryUsersRepository",
    "SqlAlchemyUsersRepository",
    "UsersRepository",
]
