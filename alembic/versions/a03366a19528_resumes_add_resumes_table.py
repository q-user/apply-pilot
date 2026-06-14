"""resumes: add resumes table

Revision ID: a03366a19528
Revises: 8c290cad3915
Create Date: 2026-06-14 21:30:00.000000

Adds the ``resumes`` table that stores user-uploaded resume files together
with the extracted plain text. The ``user_id`` column is a foreign key to
``users.id`` (owned by the auth slice, issue #11); the migration assumes
the ``users`` table already exists at upgrade time. In a fresh dev
environment the auth slice's migration runs first (alphabetically /
by revision id) so the FK resolves cleanly.

The Alembic ``env.py`` registers both the orders and the resumes model
modules, which is why ``--autogenerate`` would normally pick this up.
We hand-write the revision because the resumes model references the
``users`` table before it exists, which breaks autogenerate's table sort.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a03366a19528"
down_revision: str | Sequence[str] | None = "8c290cad3915"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "resumes",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("filename", sa.String(length=512), nullable=False),
        sa.Column("content_type", sa.String(length=127), nullable=False),
        sa.Column("size", sa.Integer(), nullable=False),
        sa.Column("raw_text", sa.Text(), nullable=False),
        sa.Column("plain_text", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_resumes_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_resumes")),
    )
    op.create_index(op.f("ix_resumes_user_id"), "resumes", ["user_id"], unique=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_resumes_user_id"), table_name="resumes")
    op.drop_table("resumes")
