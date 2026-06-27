"""Canonical Vacancy ORM model.

A ``Vacancy`` row is the application-wide representation of a job posting
ingested from an external source (hh.ru, Habr Career, Telegram channel,
etc.). All downstream features (search, scoring, dedup, applying) read
from this model rather than from the raw source payloads.

The ``(source, source_id)`` pair is unique: it is the natural key we use
to deduplicate re-imports of the same posting. The original raw payload
is preserved in :attr:`Vacancy.raw_data` so we can re-normalise when the
mapping rules change.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from apply_pilot.db import Base
from apply_pilot.shared.types import GUID


class Vacancy(Base):
    """A canonical job vacancy record, normalised from an external source."""

    __tablename__ = "vacancies"
    __table_args__ = (
        # The natural key: one canonical row per (source, external id).
        UniqueConstraint("source", "source_id", name="uq_vacancies_source_source_id"),
        # Backs ``ORDER BY created_at DESC`` on ``list_recent`` /
        # ``list_with_filters`` (sort phase).
        Index("ix_vacancies_created_at", "created_at"),
        # Backs ``list_by_source`` and the source-filtered branch of
        # ``list_with_filters``; the composite ordering lets PostgreSQL
        # satisfy both the ``WHERE`` and ``ORDER BY`` from the index.
        Index("ix_vacancies_source_created_at", "source", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=uuid.uuid4)

    # --- Provenance --------------------------------------------------------
    source: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    source_id: Mapped[str] = mapped_column(String(255), nullable=False)

    # --- Core posting fields ----------------------------------------------
    title: Mapped[str] = mapped_column(String(1024), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    url: Mapped[str | None] = mapped_column(String(2048), nullable=True)

    # --- Salary (normalised to net, monthly) -------------------------------
    salary_from: Mapped[int | None] = mapped_column(Integer, nullable=True)
    salary_to: Mapped[int | None] = mapped_column(Integer, nullable=True)
    salary_currency: Mapped[str] = mapped_column(
        String(3), nullable=False, server_default="RUR", default="RUR"
    )
    # Stored values are always NET (gross has been converted by the normaliser).
    salary_gross: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default="0", default=False
    )

    # --- Employer / location ----------------------------------------------
    employer_name: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    location: Mapped[str | None] = mapped_column(String(512), nullable=True)

    # --- Metadata ----------------------------------------------------------
    schedule: Mapped[str | None] = mapped_column(String(255), nullable=True)
    experience: Mapped[str | None] = mapped_column(String(255), nullable=True)
    skills: Mapped[list[str] | None] = mapped_column(JSON, nullable=True)

    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    source_updated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # --- Bookkeeping -------------------------------------------------------
    raw_data: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    content_hash: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), onupdate=func.now(), nullable=True
    )

    def __repr__(self) -> str:
        return (
            f"Vacancy(id={self.id!s}, source={self.source!r}, "
            f"source_id={self.source_id!r}, title={self.title!r})"
        )


__all__ = ["Vacancy"]
