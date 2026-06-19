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
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from typing import Protocol

from sqlalchemy import func as sa_func
from sqlalchemy import select
from sqlalchemy.orm import Session

from apply_pilot.features.users.models import User, UserSession


class UsersRepository(Protocol):
    """Minimal interface the auth slice relies on.

    The protocol grew ``list_all`` for the daily digest broadcast: the
    digest iterates every user that has linked a Telegram account, and
    the stats service needs a single repository that owns that list.
    """

    def create(
        self,
        *,
        email: str,
        hashed_password: str,
        is_active: bool,
        is_admin: bool = False,
    ) -> User: ...
    def get_by_id(self, user_id: uuid.UUID) -> User | None: ...
    def get_by_email(self, email: str) -> User | None: ...
    def list_all(self) -> Sequence[User]: ...


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

    def create(
        self,
        *,
        email: str,
        hashed_password: str,
        is_active: bool,
        is_admin: bool = False,
    ) -> User:
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
            is_admin=is_admin,
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

    def list_all(self) -> Sequence[User]:
        """Return every :class:`User` known to the repository.

        Order is insertion order, which mirrors how production lists
        users (e.g. ``SELECT * FROM users ORDER BY created_at`` in
        the SQL implementation). Used by the daily digest broadcast
        to enumerate the users that have linked a Telegram account.
        """
        return list(self._by_id.values())

    def list_paginated(self, *, limit: int, offset: int) -> list[User]:
        """Return up to *limit* users starting at *offset*.

        Mirror of :meth:`SqlAlchemyUsersRepository.list_paginated` so
        in-memory and SQL implementations are interchangeable. The
        order is ``created_at`` descending, then ``id`` ascending for
        stable pagination across calls.
        """
        rows = sorted(
            self._by_id.values(),
            key=lambda u: (-u.created_at.timestamp(), str(u.id)),
        )
        return rows[offset : offset + limit]

    def count(self) -> int:
        """Return the total number of users in the repository."""
        return len(self._by_id)


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

    def create(
        self,
        *,
        email: str,
        hashed_password: str,
        is_active: bool,
        is_admin: bool = False,
    ) -> User:
        normalised = email.lower()
        session = self._scope()
        try:
            user = User(
                email=normalised,
                hashed_password=hashed_password,
                is_active=is_active,
                is_admin=is_admin,
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

    def list_all(self) -> Sequence[User]:
        """Return every :class:`User` known to the database.

        Used by the daily digest broadcast to enumerate users; the
        production ordering (``created_at``) keeps the broadcast
        deterministic.
        """
        session = self._scope()
        try:
            statement = select(User).order_by(User.created_at.asc())
            return list(session.execute(statement).scalars().all())
        finally:
            if self._session is None:
                session.close()

    def list_paginated(self, *, limit: int, offset: int) -> list[User]:
        """Return up to *limit* users starting at *offset*.

        Ordered by ``created_at DESC, id ASC`` so pagination is stable
        across requests even when multiple users share a ``created_at``
        value. Used by the admin ``/admin/users`` HTML page (issue #171).
        """
        session = self._scope()
        try:
            statement = (
                select(User)
                .order_by(User.created_at.desc(), User.id.asc())
                .offset(offset)
                .limit(limit)
            )
            return list(session.execute(statement).scalars().all())
        finally:
            if self._session is None:
                session.close()

    def count(self) -> int:
        """Return the total number of users in the database.

        Used by the admin ``/admin/users`` HTML page to compute the
        total page count for the pagination footer.
        """
        session = self._scope()
        try:
            statement = select(sa_func.count()).select_from(User)
            return int(session.execute(statement).scalar_one())
        finally:
            if self._session is None:
                session.close()


# ---------------------------------------------------------------------------
# UserSession repository (M1, issue #12)
# ---------------------------------------------------------------------------


class UserSessionRepository(Protocol):
    """Minimal interface the AuthService relies on for session persistence."""

    def create(
        self, *, user_id: uuid.UUID, token_hash: str, expires_at: datetime
    ) -> UserSession: ...
    def get_by_token_hash(self, token_hash: str) -> UserSession | None: ...
    def revoke(self, *, token_hash: str, revoked_at: datetime) -> None: ...
    def list_by_user_id(self, user_id: uuid.UUID) -> list[UserSession]: ...


# ---------------------------------------------------------------------------
# In-memory UserSession implementation
# ---------------------------------------------------------------------------


class InMemoryUserSessionRepository:
    """Dict-backed session repository for tests."""

    def __init__(self) -> None:
        self._by_id: dict[uuid.UUID, UserSession] = {}
        self._by_token_hash: dict[str, uuid.UUID] = {}

    def create(self, *, user_id: uuid.UUID, token_hash: str, expires_at: datetime) -> UserSession:
        session = UserSession(
            id=uuid.uuid4(),
            user_id=user_id,
            token_hash=token_hash,
            expires_at=expires_at,
        )
        session.created_at = datetime.now(UTC)
        self._by_id[session.id] = session
        self._by_token_hash[token_hash] = session.id
        return session

    def get_by_token_hash(self, token_hash: str) -> UserSession | None:
        session_id = self._by_token_hash.get(token_hash)
        if session_id is None:
            return None
        return self._by_id.get(session_id)

    def revoke(self, *, token_hash: str, revoked_at: datetime) -> None:
        session = self.get_by_token_hash(token_hash)
        if session is not None:
            session.revoked_at = revoked_at

    def list_by_user_id(self, user_id: uuid.UUID) -> list[UserSession]:
        return [s for s in self._by_id.values() if s.user_id == user_id]


# ---------------------------------------------------------------------------
# SQLAlchemy UserSession implementation
# ---------------------------------------------------------------------------


class SqlAlchemyUserSessionRepository:
    """SQLAlchemy-backed session repository.

    Follows the same dual-construction pattern as
    :class:`SqlAlchemyUsersRepository`: pass either a ``session``
    (caller-managed) or a ``session_factory`` (per-operation lifecycle).
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
            raise RuntimeError("SqlAlchemyUserSessionRepository is not bound to a session")
        return self._session_factory()

    def create(self, *, user_id: uuid.UUID, token_hash: str, expires_at: datetime) -> UserSession:
        scoped = self._scope()
        try:
            session = UserSession(
                user_id=user_id,
                token_hash=token_hash,
                expires_at=expires_at,
            )
            scoped.add(session)
            scoped.commit()
            scoped.refresh(session)
            return session
        except Exception:
            scoped.rollback()
            raise
        finally:
            if self._session is None:
                scoped.close()

    def get_by_token_hash(self, token_hash: str) -> UserSession | None:
        scoped = self._scope()
        try:
            statement = select(UserSession).where(UserSession.token_hash == token_hash)
            return scoped.execute(statement).scalar_one_or_none()
        finally:
            if self._session is None:
                scoped.close()

    def revoke(self, *, token_hash: str, revoked_at: datetime) -> None:
        scoped = self._scope()
        try:
            statement = select(UserSession).where(UserSession.token_hash == token_hash)
            session = scoped.execute(statement).scalar_one_or_none()
            if session is not None:
                session.revoked_at = revoked_at
                scoped.commit()
        except Exception:
            scoped.rollback()
            raise
        finally:
            if self._session is None:
                scoped.close()

    def list_by_user_id(self, user_id: uuid.UUID) -> list[UserSession]:
        scoped = self._scope()
        try:
            statement = select(UserSession).where(UserSession.user_id == user_id)
            return list(scoped.execute(statement).scalars().all())
        finally:
            if self._session is None:
                scoped.close()


__all__ = [
    "InMemoryUserSessionRepository",
    "InMemoryUsersRepository",
    "SqlAlchemyUserSessionRepository",
    "SqlAlchemyUsersRepository",
    "UserSessionRepository",
    "UsersRepository",
]
