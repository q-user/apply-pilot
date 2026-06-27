"""sources: add indexes on vacancies.created_at for ORDER BY DESC queries

Revision ID: h1i2j3k4l5m6
Revises: f0e1a2b3c4d5, g7h8i9j0k1l2
Create Date: 2026-06-27 00:00:00.000000

Adds two indexes to back the ``ORDER BY created_at DESC`` paths on the
vacancies table:

* ``ix_vacancies_created_at`` — covers :meth:`SqlVacancyRepository.list_recent`
  and the sort phase of :meth:`list_with_filters` when no other index
  narrows the row set first.
* ``ix_vacancies_source_created_at`` — covers
  :meth:`list_by_source` and the source-filtered branch of
  :meth:`list_with_filters`; composite ordering lets PostgreSQL use
  the index for both the ``WHERE`` and ``ORDER BY`` clauses without
  a sort step.

Filter combinations that include ``LOWER(location) LIKE`` or
``salary_from >=`` still benefit from a dedicated
``EXPLAIN``-driven index in a follow-up migration; this one targets
the most common call sites.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "h1i2j3k4l5m6"
down_revision: str | Sequence[str] | None = "5b7c8d9e0f1a"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_index(
        op.f("ix_vacancies_created_at"),
        "vacancies",
        ["created_at"],
        unique=False,
    )
    op.create_index(
        op.f("ix_vacancies_source_created_at"),
        "vacancies",
        ["source", "created_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_vacancies_source_created_at"), table_name="vacancies")
    op.drop_index(op.f("ix_vacancies_created_at"), table_name="vacancies")
