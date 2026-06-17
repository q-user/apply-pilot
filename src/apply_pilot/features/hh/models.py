"""ORM model for the HH credentials slice.

A single ``hh_credentials`` table stores encrypted OAuth tokens from
hh.ru. One user may have at most one credential row (``user_id`` is
unique); storing a new row for an existing user overwrites the old one.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from apply_pilot.db import Base
from apply_pilot.features.users.models import GUID


class HHCredential(Base):
    """Encrypted hh.ru OAuth credentials for a user.

    Sensitive fields (``encrypted_access_token``,
    ``encrypted_refresh_token``) are stored as ciphertext only —
    the plaintext is never written to the database or logs.
    """

    __tablename__ = "hh_credentials"

    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        GUID(),
        ForeignKey("users.id", name="fk_hh_credentials_user_id"),
        unique=True,
        nullable=False,
    )
    encrypted_access_token: Mapped[str] = mapped_column(Text, nullable=False)
    encrypted_refresh_token: Mapped[str | None] = mapped_column(Text, nullable=True)
    token_type: Mapped[str] = mapped_column(
        String(50), nullable=False, default="bearer", server_default="bearer"
    )
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), onupdate=func.now(), nullable=True
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return (
            f"HHCredential(id={self.id!s}, user_id={self.user_id!s}, "
            f"encrypted_access_token=REDACTED, encrypted_refresh_token=REDACTED, "
            f"token_type={self.token_type!r}, expires_at={self.expires_at!r})"
        )


__all__ = ["HHCredential"]
