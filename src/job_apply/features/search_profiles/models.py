"""SearchProfile ORM model.

A search profile stores the criteria a user wants when searching for
jobs: keywords, salary range, preferred location, and work schedule.
Profiles can be deactivated (``is_active=False``) instead of deleted.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column

from job_apply.db import Base
from job_apply.shared.types import GUID

# Re-exported for backward compatibility — ``GUID`` used to live in this
# module. New code should import it from ``job_apply.shared``.
__all__ = ["GUID", "SearchProfile"]


class SearchProfile(Base):
    """A set of job-search criteria owned by a user."""

    __tablename__ = "search_profiles"

    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        GUID(),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    keywords: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    salary_min: Mapped[int | None] = mapped_column(Integer, nullable=True)
    salary_max: Mapped[int | None] = mapped_column(Integer, nullable=True)
    location: Mapped[str | None] = mapped_column(String(255), nullable=True)
    schedule: Mapped[str | None] = mapped_column(String(255), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), onupdate=func.now(), nullable=True
    )

    def __repr__(self) -> str:
        return f"SearchProfile(id={self.id!s}, title={self.title!r}, user_id={self.user_id!s})"
