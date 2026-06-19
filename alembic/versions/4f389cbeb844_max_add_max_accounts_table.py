"""max: add max_accounts table

Revision ID: 4f389cbeb844
Revises: a1c2d3e4f5b6
Create Date: 2026-06-19 12:00:00.000000

Adds the ``max_accounts`` table that stores the one-to-one mapping between
local user accounts and MAX messenger user identities. The table is a
1-to-1 mirror of the existing ``telegram_accounts`` table, adapted for the
MAX bot integration tracked under the M9 milestone.

Mirrors the telegram migration
(``b1d2e3f4a5b6_telegram_add_telegram_accounts_table.py``) by design: the
MAX bot slice reuses the same account-linking lifecycle (link a single
external messenger identity to a single local user). The schema differs
only in the column that holds the external user id (``max_user_id``
instead of ``telegram_user_id``) and the table/index/constraint names.

Hand-written to avoid sqlite/postgres UUID mismatches (same policy as
the telegram, users, and resumes migrations).
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "4f389cbeb844"
down_revision: str | None = "a1c2d3e4f5b6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "max_accounts",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("user_id", sa.String(length=36), nullable=False),
        sa.Column("max_user_id", sa.BigInteger(), nullable=False),
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
            name=op.f("fk_max_accounts_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_max_accounts")),
        sa.UniqueConstraint("user_id", name=op.f("uq_max_accounts_user_id")),
        sa.UniqueConstraint("max_user_id", name=op.f("uq_max_accounts_max_user_id")),
    )
    op.create_index(op.f("ix_max_accounts_user_id"), "max_accounts", ["user_id"], unique=True)
    op.create_index(
        op.f("ix_max_accounts_max_user_id"),
        "max_accounts",
        ["max_user_id"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_max_accounts_max_user_id"), table_name="max_accounts")
    op.drop_index(op.f("ix_max_accounts_user_id"), table_name="max_accounts")
    op.drop_table("max_accounts")
