"""Persistence gateway for the Telegram account linking slice.

Two repository implementations live here:

* :class:`InMemoryTelegramAccountRepository` — a dict-backed fake used by
  unit tests and useful for local experimentation.
* :class:`SqlAlchemyTelegramAccountRepository` — the production
  implementation that talks to a SQLAlchemy ``Session``.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from typing import Protocol

from sqlalchemy import select
from sqlalchemy.orm import Session

from apply_pilot.features.telegram.models import TelegramAccount


class TelegramAccountRepository(Protocol):
    """Minimal interface the telegram slice relies on.

    The protocol is intentionally narrow: every method listed here is
    used by at least one in-tree consumer, and the
    :class:`StatsService` (digest slice) only needs ``list_all`` to
    enumerate the users that have linked a Telegram account.
    """

    def create(
        self, *, user_id: uuid.UUID, telegram_user_id: int, username: str | None = None
    ) -> TelegramAccount: ...

    def list_all(self) -> Sequence[TelegramAccount]: ...

    def find_by_telegram_user_id(self, telegram_user_id: int) -> TelegramAccount | None: ...

    def find_by_user_id(self, user_id: uuid.UUID) -> TelegramAccount | None: ...


# ---------------------------------------------------------------------------
# In-memory implementation
# ---------------------------------------------------------------------------


class InMemoryTelegramAccountRepository:
    """Dict-backed repository for tests and local hacking.

    Maintains indices by user_id and telegram_user_id so duplicate checks
    mirror the database uniqueness constraints.
    """

    def __init__(self) -> None:
        self._by_id: dict[uuid.UUID, TelegramAccount] = {}
        self._by_user_id: dict[uuid.UUID, uuid.UUID] = {}
        self._by_telegram_user_id: dict[int, uuid.UUID] = {}

    def create(
        self, *, user_id: uuid.UUID, telegram_user_id: int, username: str | None = None
    ) -> TelegramAccount:
        if user_id in self._by_user_id:
            raise _DuplicateTelegramAccountError(
                f"user {user_id} already has a linked Telegram account"
            )
        if telegram_user_id in self._by_telegram_user_id:
            raise _DuplicateTelegramAccountError(
                f"Telegram user {telegram_user_id} is already linked"
            )
        account = TelegramAccount(
            id=uuid.uuid4(),
            user_id=user_id,
            telegram_user_id=telegram_user_id,
            username=username,
        )
        account.linked_at = datetime.now(UTC)
        self._by_id[account.id] = account
        self._by_user_id[user_id] = account.id
        self._by_telegram_user_id[telegram_user_id] = account.id
        return account

    def list_all(self) -> Sequence[TelegramAccount]:
        """Return every linked :class:`TelegramAccount`.

        The digest slice iterates the result to compute per-user stats
        and broadcast a Telegram message; order is insertion order so
        the broadcast is deterministic within a single process.
        """
        return list(self._by_id.values())

    def find_by_telegram_user_id(self, telegram_user_id: int) -> TelegramAccount | None:
        """Return the linked :class:`TelegramAccount` for ``telegram_user_id``.

        Used by command handlers (e.g. ``/reject``) that receive the
        Telegram user id from an incoming update and need to resolve
        the local user behind it. Returns ``None`` if the Telegram id
        is not linked.
        """
        account_id = self._by_telegram_user_id.get(telegram_user_id)
        if account_id is None:
            return None
        return self._by_id.get(account_id)

    def find_by_user_id(self, user_id: uuid.UUID) -> TelegramAccount | None:
        """Return the linked :class:`TelegramAccount` for the local ``user_id``.

        Used by the apply-worker notifier (M5, issue #50) to resolve
        the Telegram chat id of a user that just had an :class:`ApplyJob`
        transition to a terminal state. The repository enforces the
        one-account-per-user invariant at write time, so the lookup
        here can never return more than one row.
        """
        account_id = self._by_user_id.get(user_id)
        if account_id is None:
            return None
        return self._by_id.get(account_id)


class _DuplicateTelegramAccountError(Exception):
    """Internal sentinel raised by the in-memory repo on collisions."""


# ---------------------------------------------------------------------------
# SQLAlchemy implementation
# ---------------------------------------------------------------------------


class SqlAlchemyTelegramAccountRepository:
    """SQLAlchemy-backed repository.

    The repository can be constructed two ways:

    * With a single ``Session`` (caller-managed lifetime). Useful for
      script-style use cases that already hold an open session.
    * With a ``session_factory`` (default). The repository opens a
      short-lived session per operation and closes it before
      returning.
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
            raise RuntimeError("SqlAlchemyTelegramAccountRepository is not bound to a session")
        return self._session_factory()

    def create(
        self, *, user_id: uuid.UUID, telegram_user_id: int, username: str | None = None
    ) -> TelegramAccount:
        scoped = self._scope()
        try:
            # Check for existing linkage by user_id
            existing = scoped.execute(
                select(TelegramAccount).where(TelegramAccount.user_id == user_id)
            ).scalar_one_or_none()
            if existing is not None:
                raise _DuplicateTelegramAccountError(
                    f"user {user_id} already has a linked Telegram account"
                )
            account = TelegramAccount(
                user_id=user_id,
                telegram_user_id=telegram_user_id,
                username=username,
            )
            scoped.add(account)
            scoped.commit()
            scoped.refresh(account)
            return account
        except _DuplicateTelegramAccountError:
            scoped.rollback()
            raise
        except Exception:
            scoped.rollback()
            raise
        finally:
            if self._session is None:
                scoped.close()

    def list_all(self) -> Sequence[TelegramAccount]:
        """Return every linked :class:`TelegramAccount`.

        Read-only and side-effect free; used by the digest slice to
        enumerate the users that should receive a daily digest.
        """
        scoped = self._scope()
        try:
            statement = select(TelegramAccount).order_by(TelegramAccount.linked_at.asc())
            return list(scoped.execute(statement).scalars().all())
        finally:
            if self._session is None:
                scoped.close()

    def find_by_telegram_user_id(self, telegram_user_id: int) -> TelegramAccount | None:
        """Return the linked :class:`TelegramAccount` for ``telegram_user_id``.

        Used by command handlers (e.g. ``/reject``) that receive the
        Telegram user id from an incoming update and need to resolve
        the local user behind it. Returns ``None`` if the Telegram id
        is not linked.
        """
        scoped = self._scope()
        try:
            statement = select(TelegramAccount).where(
                TelegramAccount.telegram_user_id == telegram_user_id
            )
            return scoped.execute(statement).scalar_one_or_none()
        finally:
            if self._session is None:
                scoped.close()

    def find_by_user_id(self, user_id: uuid.UUID) -> TelegramAccount | None:
        """Return the linked :class:`TelegramAccount` for the local ``user_id``.

        Mirrors the in-memory repo's behaviour. The
        ``TelegramAccount.user_id`` column is ``UNIQUE`` so the lookup
        is unambiguous. Used by the apply-worker notifier (M5, issue
        #50) to resolve the Telegram chat id of a user whose
        :class:`ApplyJob` just reached a terminal state.
        """
        scoped = self._scope()
        try:
            statement = select(TelegramAccount).where(TelegramAccount.user_id == user_id)
            return scoped.execute(statement).scalar_one_or_none()
        finally:
            if self._session is None:
                scoped.close()


__all__ = [
    "InMemoryTelegramAccountRepository",
    "SqlAlchemyTelegramAccountRepository",
    "TelegramAccountRepository",
]
