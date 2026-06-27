"""ORM model for the auth slice.

This is the *public* User model other M1 slices will reference via
``from apply_pilot.features.users import User``. The field set is
intentionally stable: any new field must be additive and must not
break consumers that read the existing attributes.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    String,
    func,
)
from sqlalchemy import (
    text as sa_text,
)
from sqlalchemy.orm import Mapped, mapped_column

from apply_pilot.db import Base
from apply_pilot.shared.types import GUID  # noqa: F401  # re-export for backward compat


class User(Base):
    """An authenticated user of the system.

    Public surface (kept stable for downstream slices):

    * ``id``: UUID primary key.
    * ``email``: unique, normalised lowercase at the service layer.
    * ``hashed_password``: never the plaintext; verification lives in
      :mod:`apply_pilot.features.users.security`.
    * ``is_active``: soft-disable flag; inactive users cannot log in.
    * ``is_admin``: whether this user may access the ``/admin/*`` surface.
      Defaults to ``False``; the first registered user is NOT
      automatically an admin — an operator must promote them with the
      ``apply-pilot promote --email <email>`` CLI. There is no
      self-service path to become an admin; this keeps the public
      signup surface unprivileged.
    * ``created_at`` / ``updated_at``: server-side timestamps.
    """

    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=uuid.uuid4)
    email: Mapped[str] = mapped_column(String(255), unique=True, index=True, nullable=False)
    hashed_password: Mapped[str] = mapped_column(String(512), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    # NOTE: the ``server_default`` is the canonical Postgres-compatible
    # spelling (``sa.text("false")``); the Python-side ``default`` is a
    # belt-and-suspenders fallback for code paths that bypass Alembic
    # (e.g. ``Base.metadata.create_all`` in the sqlite test harness).
    is_admin: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
        server_default=sa_text("false"),
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), onupdate=func.now(), nullable=True
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return (
            f"User(id={self.id!s}, email={self.email!r}, "
            f"is_active={self.is_active}, is_admin={self.is_admin})"
        )


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
