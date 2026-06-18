"""sources: add vacancies table

Revision ID: c23d4a1b2e3f
Revises: b17c001a19528
Create Date: 2026-06-15 00:00:00.000000

Adds the ``vacancies`` table that stores the canonical, normalised form
of a job posting ingested from an external source (hh.ru, Habr Career,
Telegram channel, …). The natural key ``(source, source_id)`` is unique
so repeated imports of the same posting hit an ``ON CONFLICT DO UPDATE``
path in the repository rather than creating duplicates.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c23d4a1b2e3f"
down_revision: str | Sequence[str] | None = "b17c001a19528"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "vacancies",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("source", sa.String(length=50), nullable=False),
        sa.Column("source_id", sa.String(length=255), nullable=False),
        sa.Column("title", sa.String(length=1024), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("url", sa.String(length=2048), nullable=True),
        sa.Column("salary_from", sa.Integer(), nullable=True),
        sa.Column("salary_to", sa.Integer(), nullable=True),
        sa.Column(
            "salary_currency",
            sa.String(length=3),
            nullable=False,
            server_default="RUR",
        ),
        sa.Column(
            "salary_gross",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
        sa.Column("employer_name", sa.String(length=1024), nullable=True),
        sa.Column("location", sa.String(length=512), nullable=True),
        sa.Column("schedule", sa.String(length=255), nullable=True),
        sa.Column("experience", sa.String(length=255), nullable=True),
        sa.Column("skills", sa.JSON(), nullable=True),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("source_updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("raw_data", sa.JSON(), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=False,
        ),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_vacancies")),
        sa.UniqueConstraint("source", "source_id", name=op.f("uq_vacancies_source_source_id")),
    )
    op.create_index(op.f("ix_vacancies_source"), "vacancies", ["source"], unique=False)
    op.create_index(op.f("ix_vacancies_content_hash"), "vacancies", ["content_hash"], unique=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_vacancies_content_hash"), table_name="vacancies")
    op.drop_index(op.f("ix_vacancies_source"), table_name="vacancies")
    op.drop_table("vacancies")
