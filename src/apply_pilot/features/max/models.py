"""ORM model for MAX messenger account linking.

Stored in the max slice so the auth slice stays free of
cross-cutting knowledge about the MAX integration.

Mirrors :mod:`apply_pilot.features.telegram.models` by design — both
slices follow the same one-to-one ``local user <-> external messenger
identity`` linking lifecycle. The schema differs only in the column that
holds the external user id (``max_user_id`` instead of
``telegram_user_id``) and the table/index/constraint names; the
behavioural surface is intentionally identical so the channel-agnostic
``messaging`` slice can plug in either implementation interchangeably.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import BigInteger, DateTime, ForeignKey, String, func
from sqlalchemy.orm import Mapped, mapped_column

from apply_pilot.db import Base
from apply_pilot.features.users.models import GUID


class MaxAccount(Base):
    """Links a local user to their MAX messenger account.

    * ``id``: UUID primary key.
    * ``user_id``: FK to ``users.id``, unique — a user can link at most one
      MAX account.
    * ``max_user_id``: the MAX-level user id (bigint), unique — a MAX
      account can be linked to at most one local user.
    * ``username``: optional MAX @username at link time (informational).
    * ``linked_at``: server-side timestamp of the linking event.
    """

    __tablename__ = "max_accounts"

    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        GUID(),
        ForeignKey("users.id", ondelete="CASCADE"),
        unique=True,
        nullable=False,
        index=True,
    )
    max_user_id: Mapped[int] = mapped_column(BigInteger, unique=True, nullable=False, index=True)
    username: Mapped[str | None] = mapped_column(String(255), nullable=True)
    linked_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return (
            f"MaxAccount(id={self.id!s}, user_id={self.user_id!s}, "
            f"max_user_id={self.max_user_id!r})"
        )


__all__ = ["MaxAccount"]
