"""ORM model for the prompt-version registry.

The ``prompt_versions`` table stores every version of every prompt
template the application uses (e.g. ``vacancy_scoring``,
``cover_letter``). Each row records:

* ``name`` — the prompt family (``"vacancy_scoring"``,
  ``"cover_letter"``).
* ``version`` — the SemVer string for this revision
  (``"1.0.0"``, ``"1.2.0-rc.1"``).
* ``template`` — the actual prompt body the LLM scoring pass will
  substitute variables into.
* ``is_active`` — exactly one row per ``name`` is allowed to be
  active; the DB enforces this with a partial UNIQUE INDEX.
* ``created_at`` — server-side timestamp with timezone.

Schema notes
------------

* ``UNIQUE(name, version)`` prevents accidental duplicate revisions.
* A partial UNIQUE INDEX on ``(name) WHERE is_active`` enforces the
  "one active version per name" invariant at the DB level (sqlite
  supports ``WHERE`` on indexes; PostgreSQL does too). The application
  layer keeps the invariant consistent in the in-memory implementation
  via :meth:`InMemoryPromptVersionRegistry.set_active`.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    Index,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from job_apply.db import Base
from job_apply.shared.types import GUID


class PromptVersionRow(Base):
    """A persisted prompt version belonging to a prompt family.

    The same name can accumulate many versions over time; only one of
    them is "active" at any moment and that active version is what the
    future LLM scoring pass (issue #29) will pick up.
    """

    __tablename__ = "prompt_versions"
    __table_args__ = (
        UniqueConstraint("name", "version", name="uq_prompt_versions_name_version"),
        # Partial unique index — only one ``is_active`` row per name.
        # The ``WHERE`` clause is the partial index predicate: both
        # sqlite and PostgreSQL support it.
        Index(
            "ix_prompt_versions_one_active_per_name",
            "name",
            unique=True,
            sqlite_where=text("is_active"),
            postgresql_where=text("is_active"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(GUID(), primary_key=True, default=uuid.uuid4)

    name: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    version: Mapped[str] = mapped_column(String(50), nullable=False)
    template: Mapped[str] = mapped_column(Text, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return (
            f"PromptVersionRow(id={self.id!s}, name={self.name!r}, "
            f"version={self.version!r}, is_active={self.is_active!s})"
        )


__all__ = ["PromptVersionRow"]
