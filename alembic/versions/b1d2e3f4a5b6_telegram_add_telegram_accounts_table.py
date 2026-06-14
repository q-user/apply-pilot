"""telegram: add telegram_accounts table

Revision ID: b1d2e3f4a5b6
Revises: a03366a19528
Create Date: 2026-06-15 20:00:00.000000

Adds the ``telegram_accounts`` table that stores the one-to-one mapping
between local user accounts and Telegram user identities. The table lives
in the telegram slice but references ``users.id`` via a foreign key.

Hand-written to avoid sqlite/postgres UUID mismatches (same policy as the
users and resumes migrations).
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b1d2e3f4a5b6"
down_revision: str | None = "a03366a19528"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "telegram_accounts",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("user_id", sa.String(length=36), nullable=False),
        sa.Column("telegram_user_id", sa.BigInteger(), nullable=False),
        sa.Column("username", sa.String(length=255), nullable=True),
        sa.Column(
            "linked_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_telegram_accounts_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_telegram_accounts")),
        sa.UniqueConstraint("user_id", name=op.f("uq_telegram_accounts_user_id")),
        sa.UniqueConstraint("telegram_user_id", name=op.f("uq_telegram_accounts_telegram_user_id")),
    )
    op.create_index(
        op.f("ix_telegram_accounts_user_id"), "telegram_accounts", ["user_id"], unique=True
    )
    op.create_index(
        op.f("ix_telegram_accounts_telegram_user_id"),
        "telegram_accounts",
        ["telegram_user_id"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_telegram_accounts_telegram_user_id"), table_name="telegram_accounts")
    op.drop_index(op.f("ix_telegram_accounts_user_id"), table_name="telegram_accounts")
    op.drop_table("telegram_accounts")
