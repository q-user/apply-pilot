"""search_profiles: add search_profiles table

Revision ID: b17c001a19528
Revises: a03366a19528
Create Date: 2026-06-15 00:00:00.000000

Adds the ``search_profiles`` table that stores user search criteria
(keywords, salary range, location, schedule). The ``user_id`` column is
a foreign key to ``users.id`` (owned by the auth slice, issue #11);
the migration assumes the ``users`` table already exists at upgrade time.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b17c001a19528"
down_revision: str | Sequence[str] | None = "a03366a19528"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "search_profiles",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("user_id", sa.String(length=36), nullable=False),
        sa.Column("title", sa.String(length=255), nullable=False),
        sa.Column("keywords", sa.String(length=1024), nullable=True),
        sa.Column("salary_min", sa.Integer(), nullable=True),
        sa.Column("salary_max", sa.Integer(), nullable=True),
        sa.Column("location", sa.String(length=255), nullable=True),
        sa.Column("schedule", sa.String(length=255), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=False,
        ),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_search_profiles_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_search_profiles")),
    )
    op.create_index(
        op.f("ix_search_profiles_user_id"), "search_profiles", ["user_id"], unique=False
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_search_profiles_user_id"), table_name="search_profiles")
    op.drop_table("search_profiles")
