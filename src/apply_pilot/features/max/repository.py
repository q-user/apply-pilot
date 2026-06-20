"""Persistence gateway for the MAX account linking slice.

Two repository implementations live here:

* :class:`InMemoryMaxAccountRepository` — a dict-backed fake used by
  unit tests and useful for local experimentation.
* :class:`SqlAlchemyMaxAccountRepository` — the production
  implementation that talks to a SQLAlchemy ``Session``.

Mirrors :mod:`apply_pilot.features.telegram.repository` by design.
``find_by_external_user_id`` is a thin alias for
:func:`find_by_max_user_id` so the concrete repository satisfies the
:class:`apply_pilot.features.messaging.protocols.MessagingAccountRepository`
Protocol structurally without renaming the channel-specific lookup.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from typing import Protocol

from sqlalchemy import select
from sqlalchemy.orm import Session

from apply_pilot.features.max.models import MaxAccount


class MaxAccountRepository(Protocol):
    """Minimal interface the max slice relies on.

    The protocol is intentionally narrow: every method listed here is
    used by at least one in-tree consumer, mirroring the Telegram
    repository Protocol so future MAX-side features (linking, notifier,
    digest) can drop in cleanly.
    """

    def create(
        self, *, user_id: uuid.UUID, max_user_id: int, username: str | None = None
    ) -> MaxAccount: ...

    def list_all(self) -> Sequence[MaxAccount]: ...

    def find_by_max_user_id(self, max_user_id: int) -> MaxAccount | None: ...

    def find_by_user_id(self, user_id: uuid.UUID) -> MaxAccount | None: ...


# ---------------------------------------------------------------------------
# In-memory implementation
# ---------------------------------------------------------------------------


class InMemoryMaxAccountRepository:
    """Dict-backed repository for tests and local hacking.

    Maintains indices by user_id and max_user_id so duplicate checks
    mirror the database uniqueness constraints.
    """

    def __init__(self) -> None:
        self._by_id: dict[uuid.UUID, MaxAccount] = {}
        self._by_user_id: dict[uuid.UUID, uuid.UUID] = {}
        self._by_max_user_id: dict[int, uuid.UUID] = {}

    def create(
        self, *, user_id: uuid.UUID, max_user_id: int, username: str | None = None
    ) -> MaxAccount:
        if user_id in self._by_user_id:
            raise _DuplicateMaxAccountError(f"user {user_id} already has a linked MAX account")
        if max_user_id in self._by_max_user_id:
            raise _DuplicateMaxAccountError(f"MAX user {max_user_id} is already linked")
        account = MaxAccount(
            id=uuid.uuid4(),
            user_id=user_id,
            max_user_id=max_user_id,
            username=username,
        )
        account.linked_at = datetime.now(UTC)
        self._by_id[account.id] = account
        self._by_user_id[user_id] = account.id
        self._by_max_user_id[max_user_id] = account.id
        return account

    def list_all(self) -> Sequence[MaxAccount]:
        """Return every linked :class:`MaxAccount`.

        Order is insertion order so a broadcast iteration is
        deterministic within a single process.
        """
        return list(self._by_id.values())

    def find_by_max_user_id(self, max_user_id: int) -> MaxAccount | None:
        """Return the linked :class:`MaxAccount` for ``max_user_id``.

        Used by command handlers that receive the MAX user id from an
        incoming update and need to resolve the local user behind it.
        Returns ``None`` if the MAX id is not linked.
        """
        account_id = self._by_max_user_id.get(max_user_id)
        if account_id is None:
            return None
        return self._by_id.get(account_id)

    def find_by_external_user_id(self, external_user_id: int) -> MaxAccount | None:
        """Channel-agnostic alias for :meth:`find_by_max_user_id`.

        Satisfies :class:`apply_pilot.features.messaging.protocols.MessagingAccountRepository`
        so the channel-agnostic action handlers can resolve a linked
        account without depending on the MAX-specific method name.
        """
        return self.find_by_max_user_id(external_user_id)

    def find_by_user_id(self, user_id: uuid.UUID) -> MaxAccount | None:
        """Return the linked :class:`MaxAccount` for the local ``user_id``.

        The repository enforces the one-account-per-user invariant at
        write time, so the lookup here can never return more than one
        row.
        """
        account_id = self._by_user_id.get(user_id)
        if account_id is None:
            return None
        return self._by_id.get(account_id)


class _DuplicateMaxAccountError(Exception):
    """Internal sentinel raised by the in-memory repo on collisions."""


# ---------------------------------------------------------------------------
# SQLAlchemy implementation
# ---------------------------------------------------------------------------


class SqlAlchemyMaxAccountRepository:
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
            raise RuntimeError("SqlAlchemyMaxAccountRepository is not bound to a session")
        return self._session_factory()

    def create(
        self, *, user_id: uuid.UUID, max_user_id: int, username: str | None = None
    ) -> MaxAccount:
        scoped = self._scope()
        try:
            # Check for existing linkage by user_id
            existing = scoped.execute(
                select(MaxAccount).where(MaxAccount.user_id == user_id)
            ).scalar_one_or_none()
            if existing is not None:
                raise _DuplicateMaxAccountError(f"user {user_id} already has a linked MAX account")
            account = MaxAccount(
                user_id=user_id,
                max_user_id=max_user_id,
                username=username,
            )
            scoped.add(account)
            scoped.commit()
            scoped.refresh(account)
            return account
        except _DuplicateMaxAccountError:
            scoped.rollback()
            raise
        except Exception:
            scoped.rollback()
            raise
        finally:
            if self._session is None:
                scoped.close()

    def list_all(self) -> Sequence[MaxAccount]:
        """Return every linked :class:`MaxAccount`.

        Read-only and side-effect free.
        """
        scoped = self._scope()
        try:
            statement = select(MaxAccount).order_by(MaxAccount.linked_at.asc())
            return list(scoped.execute(statement).scalars().all())
        finally:
            if self._session is None:
                scoped.close()

    def find_by_max_user_id(self, max_user_id: int) -> MaxAccount | None:
        """Return the linked :class:`MaxAccount` for ``max_user_id``.

        Used by command handlers that receive the MAX user id from an
        incoming update and need to resolve the local user behind it.
        Returns ``None`` if the MAX id is not linked.
        """
        scoped = self._scope()
        try:
            statement = select(MaxAccount).where(MaxAccount.max_user_id == max_user_id)
            return scoped.execute(statement).scalar_one_or_none()
        finally:
            if self._session is None:
                scoped.close()

    def find_by_external_user_id(self, external_user_id: int) -> MaxAccount | None:
        """Channel-agnostic alias for :meth:`find_by_max_user_id`.

        Satisfies :class:`apply_pilot.features.messaging.protocols.MessagingAccountRepository`
        so the channel-agnostic action handlers can resolve a linked
        account without depending on the MAX-specific method name.
        """
        return self.find_by_max_user_id(external_user_id)

    def find_by_user_id(self, user_id: uuid.UUID) -> MaxAccount | None:
        """Return the linked :class:`MaxAccount` for the local ``user_id``.

        Mirrors the in-memory repo's behaviour. The
        ``MaxAccount.user_id`` column is ``UNIQUE`` so the lookup is
        unambiguous.
        """
        scoped = self._scope()
        try:
            statement = select(MaxAccount).where(MaxAccount.user_id == user_id)
            return scoped.execute(statement).scalar_one_or_none()
        finally:
            if self._session is None:
                scoped.close()


__all__ = [
    "InMemoryMaxAccountRepository",
    "MaxAccountRepository",
    "SqlAlchemyMaxAccountRepository",
]
