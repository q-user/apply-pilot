"""add users table

Revision ID: 8c290cad3915
Revises: c31323bea8d1
Create Date: 2026-06-14 22:25:57.069961

Hand-written to mirror :class:`apply_pilot.features.users.models.User`.
We deliberately do not autogenerate: the dev DB is sqlite, but the
production target is Postgres, and Alembic's sqlite-flavoured output
for the ``UUID`` column would be wrong on the other side. The hand
picked ``sa.String(36)`` matches the slice's :class:`GUID` decorator
fallback path; Postgres deployments will get the native ``UUID`` type
once ``env.py`` is updated to import the model and autogenerate is
re-run on a real Postgres database.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "8c290cad3915"
down_revision: str | None = "c31323bea8d1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("email", sa.String(length=255), nullable=False),
        sa.Column("hashed_password", sa.String(length=512), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=False,
        ),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("email", name="uq_users_email"),
    )
    op.create_index(op.f("ix_users_email"), "users", ["email"], unique=True)


def downgrade() -> None:
    op.drop_index(op.f("ix_users_email"), table_name="users")
    op.drop_table("users")
