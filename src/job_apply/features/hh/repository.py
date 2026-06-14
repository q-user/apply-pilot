"""Persistence gateway for the HH credentials slice.

Two implementations:

* :class:`InMemoryHHCredentialRepository` — dict-backed fake for unit tests.
* :class:`SqlHHCredentialRepository` — SQLAlchemy-backed production implementation.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Protocol

from sqlalchemy import delete, select, update
from sqlalchemy.orm import Session

from job_apply.features.hh.models import HHCredential


class HHCredentialRepository(Protocol):
    """Minimal interface the HHCredentialService relies on."""

    def store(self, credential: HHCredential) -> HHCredential: ...

    def get_by_user_id(self, user_id: uuid.UUID) -> HHCredential | None: ...

    def update(self, credential: HHCredential) -> HHCredential: ...

    def delete(self, user_id: uuid.UUID) -> None: ...


# ---------------------------------------------------------------------------
# In-memory implementation
# ---------------------------------------------------------------------------


class InMemoryHHCredentialRepository:
    """Dict-backed repository for tests.

    A fresh instance per test keeps isolation simple.
    """

    def __init__(self) -> None:
        self._by_id: dict[uuid.UUID, HHCredential] = {}
        self._by_user_id: dict[uuid.UUID, uuid.UUID] = {}

    def store(self, credential: HHCredential) -> HHCredential:
        # If a credential for this user already exists, remove it first
        # (the service handles the "upsert" semantics).
        existing_id = self._by_user_id.get(credential.user_id)
        if existing_id is not None:
            self._by_id.pop(existing_id, None)
            self._by_user_id.pop(credential.user_id, None)

        if credential.id is None:
            credential.id = uuid.uuid4()
        if credential.created_at is None:
            credential.created_at = datetime.now(UTC)

        self._by_id[credential.id] = credential
        self._by_user_id[credential.user_id] = credential.id
        return credential

    def get_by_user_id(self, user_id: uuid.UUID) -> HHCredential | None:
        cred_id = self._by_user_id.get(user_id)
        if cred_id is None:
            return None
        return self._by_id.get(cred_id)

    def update(self, credential: HHCredential) -> HHCredential:
        existing_id = self._by_user_id.get(credential.user_id)
        if existing_id is None:
            raise ValueError("cannot update non-existent credential")
        credential.id = existing_id
        credential.updated_at = datetime.now(UTC)
        self._by_id[existing_id] = credential
        return credential

    def delete(self, user_id: uuid.UUID) -> None:
        cred_id = self._by_user_id.pop(user_id, None)
        if cred_id is not None:
            self._by_id.pop(cred_id, None)


# ---------------------------------------------------------------------------
# SQLAlchemy implementation
# ---------------------------------------------------------------------------


class SqlHHCredentialRepository:
    """SQLAlchemy-backed repository.

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
            raise RuntimeError("SqlHHCredentialRepository is not bound to a session")
        return self._session_factory()

    def store(self, credential: HHCredential) -> HHCredential:
        scoped = self._scope()
        try:
            # Upsert: delete any existing row for this user_id, then insert
            existing = scoped.execute(
                select(HHCredential).where(HHCredential.user_id == credential.user_id)
            ).scalar_one_or_none()
            if existing is not None:
                scoped.execute(
                    delete(HHCredential).where(HHCredential.user_id == credential.user_id)
                )
                scoped.flush()

            scoped.add(credential)
            scoped.commit()
            scoped.refresh(credential)
            return credential
        except Exception:
            scoped.rollback()
            raise
        finally:
            if self._session is None:
                scoped.close()

    def get_by_user_id(self, user_id: uuid.UUID) -> HHCredential | None:
        scoped = self._scope()
        try:
            statement = select(HHCredential).where(HHCredential.user_id == user_id)
            return scoped.execute(statement).scalar_one_or_none()
        finally:
            if self._session is None:
                scoped.close()

    def update(self, credential: HHCredential) -> HHCredential:
        scoped = self._scope()
        try:
            statement = (
                update(HHCredential)
                .where(HHCredential.user_id == credential.user_id)
                .values(
                    encrypted_access_token=credential.encrypted_access_token,
                    encrypted_refresh_token=credential.encrypted_refresh_token,
                    token_type=credential.token_type,
                    expires_at=credential.expires_at,
                )
            )
            scoped.execute(statement)
            scoped.commit()
            # Re-fetch to get the refreshed row
            refreshed = scoped.execute(
                select(HHCredential).where(HHCredential.user_id == credential.user_id)
            ).scalar_one_or_none()
            if refreshed is None:
                raise RuntimeError("HHCredential disappeared during update")
            return refreshed
        except Exception:
            scoped.rollback()
            raise
        finally:
            if self._session is None:
                scoped.close()

    def delete(self, user_id: uuid.UUID) -> None:
        scoped = self._scope()
        try:
            scoped.execute(delete(HHCredential).where(HHCredential.user_id == user_id))
            scoped.commit()
        except Exception:
            scoped.rollback()
            raise
        finally:
            if self._session is None:
                scoped.close()


__all__ = [
    "HHCredentialRepository",
    "InMemoryHHCredentialRepository",
    "SqlHHCredentialRepository",
]
