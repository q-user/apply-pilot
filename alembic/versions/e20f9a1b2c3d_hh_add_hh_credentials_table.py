"""hh: add hh_credentials table

Revision ID: e20f9a1b2c3d
Revises: a03366a19528
Create Date: 2026-06-15 22:00:00.000000

Adds the ``hh_credentials`` table for storing encrypted hh.ru OAuth
tokens.  The ``user_id`` column is a foreign key to ``users.id`` (owned
by the auth slice, issue #11) and is unique — one credential row per
user.

Hand-written to avoid sqlite/postgres UUID mismatches (same policy as
the users and resumes migrations).
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "e20f9a1b2c3d"
down_revision: str | Sequence[str] | None = "a03366a19528"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "hh_credentials",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("user_id", sa.String(length=36), nullable=False),
        sa.Column("encrypted_access_token", sa.Text(), nullable=False),
        sa.Column("encrypted_refresh_token", sa.Text(), nullable=True),
        sa.Column(
            "token_type",
            sa.String(length=50),
            nullable=False,
            server_default="bearer",
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=False,
        ),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id", name="uq_hh_credentials_user_id"),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name="fk_hh_credentials_user_id",
        ),
    )
    op.create_index(
        op.f("ix_hh_credentials_user_id"),
        "hh_credentials",
        ["user_id"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_hh_credentials_user_id"), table_name="hh_credentials")
    op.drop_table("hh_credentials")
