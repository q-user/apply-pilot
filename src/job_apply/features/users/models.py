"""ORM model for the auth slice.

This is the *public* User model other M1 slices will reference via
``from job_apply.features.users import User``. The field set is
intentionally stable: any new field must be additive and must not
break consumers that read the existing attributes.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import CHAR, Boolean, DateTime, String, TypeDecorator, func
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.engine.interfaces import Dialect
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql.type_api import TypeEngine

from job_apply.db import Base


class GUID(TypeDecorator):
    """Platform-independent UUID column.

    Stores values as ``CHAR(36)`` on sqlite (and other backends without
    a native UUID type) and as the native ``UUID`` type on PostgreSQL.
    This keeps the slice runnable in the in-memory sqlite tests while
    still using the production-grade type once we cut over to Postgres.
    """

    impl: type[CHAR] = CHAR
    cache_ok = True

    def load_dialect_impl(self, dialect: Dialect) -> TypeEngine[Any]:
        if dialect.name == "postgresql":
            return dialect.type_descriptor(PG_UUID(as_uuid=True))
        return dialect.type_descriptor(CHAR(36))

    def process_bind_param(self, value: object, dialect: Dialect) -> str | uuid.UUID | None:
        if value is None:
            return None
        if not isinstance(value, uuid.UUID):
            value = uuid.UUID(str(value))
        if dialect.name == "postgresql":
            return value
        return str(value)

    def process_result_value(self, value: object, dialect: Dialect) -> uuid.UUID | None:
        if value is None:
            return None
        if isinstance(value, uuid.UUID):
            return value
        return uuid.UUID(str(value))


class User(Base):
    """An authenticated user of the system.

    Public surface (kept stable for downstream slices):

    * ``id``: UUID primary key.
    * ``email``: unique, normalised lowercase at the service layer.
    * ``hashed_password``: never the plaintext; verification lives in
      :mod:`job_apply.features.users.security`.
    * ``is_active``: soft-disable flag; inactive users cannot log in.
    * ``created_at`` / ``updated_at``: server-side timestamps.
    """

    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=uuid.uuid4)
    email: Mapped[str] = mapped_column(String(255), unique=True, index=True, nullable=False)
    hashed_password: Mapped[str] = mapped_column(String(512), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), onupdate=func.now(), nullable=True
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"User(id={self.id!s}, email={self.email!r}, is_active={self.is_active})"


class UserSession(Base):
    """A persisted bearer-token session.

    Each row maps a hashed bearer token to a user. The raw token is never
    stored — only ``token_hash`` (SHA-256 hex digest of the raw token) is
    persisted, so a database dump alone does not let an attacker
    impersonate a user.

    Public surface (kept stable):

    * ``id``: UUID primary key.
    * ``user_id``: FK to :class:`User`.
    * ``token_hash``: SHA-256 hex digest of the raw bearer token.
    * ``created_at``: server-side timestamp.
    * ``expires_at``: wall-clock time when the session becomes invalid.
    * ``revoked_at``: ``None`` for active sessions; set on logout / refresh.
    """

    __tablename__ = "user_sessions"

    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        GUID(),
        # FK is declared in the Alembic migration; we keep the model
        # self-contained so sqlite in-memory tests can still create
        # the table via ``Base.metadata.create_all``.
        nullable=False,
    )
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, default=None
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return (
            f"UserSession(id={self.id!s}, user_id={self.user_id!s}, "
            f"expires_at={self.expires_at!r}, revoked_at={self.revoked_at!r})"
        )


__all__ = ["GUID", "User", "UserSession"]
