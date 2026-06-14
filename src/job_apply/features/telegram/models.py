"""ORM model for Telegram account linking.

Stored in the telegram slice so the auth slice stays free of
cross-cutting knowledge about the Telegram integration.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import BigInteger, DateTime, ForeignKey, String, func
from sqlalchemy.orm import Mapped, mapped_column

from job_apply.db import Base
from job_apply.features.users.models import GUID


class TelegramAccount(Base):
    """Links a local user to their Telegram account.

    * ``id``: UUID primary key.
    * ``user_id``: FK to ``users.id``, unique — a user can link at most one
      Telegram account.
    * ``telegram_user_id``: the Telegram-level user id (bigint), unique —
      a Telegram account can be linked to at most one local user.
    * ``username``: optional Telegram @username at link time (informational).
    * ``linked_at``: server-side timestamp of the linking event.
    """

    __tablename__ = "telegram_accounts"

    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        GUID(),
        ForeignKey("users.id", ondelete="CASCADE"),
        unique=True,
        nullable=False,
        index=True,
    )
    telegram_user_id: Mapped[int] = mapped_column(
        BigInteger, unique=True, nullable=False, index=True
    )
    username: Mapped[str | None] = mapped_column(String(255), nullable=True)
    linked_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return (
            f"TelegramAccount(id={self.id!s}, user_id={self.user_id!s}, "
            f"telegram_user_id={self.telegram_user_id!r})"
        )


__all__ = ["TelegramAccount"]
