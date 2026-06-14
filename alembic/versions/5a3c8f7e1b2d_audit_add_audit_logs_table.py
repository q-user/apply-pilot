"""audit: add audit_logs table

Revision ID: 5a3c8f7e1b2d
Revises: a03366a19528
Create Date: 2026-06-15 12:00:00.000000

Adds the ``audit_logs`` table for append-only event tracking. The
``user_id`` column references ``users.id`` logically but no
``ForeignKey`` constraint is enforced so that audit history survives
user deletion.  The column is nullable — anonymous events (e.g. a
failed login for an unknown email) can still be recorded.

The Alembic ``env.py`` registers the audit model module; this revision
is hand-written because the model references ``users.id`` which exists
from an earlier migration (8c290cad3915).
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "5a3c8f7e1b2d"
down_revision: str | Sequence[str] | None = "a03366a19528"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "audit_logs",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("event_type", sa.String(length=50), nullable=False),
        sa.Column("user_id", sa.String(length=36), nullable=True),
        sa.Column("details", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_audit_logs")),
    )
    op.create_index(op.f("ix_audit_logs_event_type"), "audit_logs", ["event_type"], unique=False)
    op.create_index(op.f("ix_audit_logs_user_id"), "audit_logs", ["user_id"], unique=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_audit_logs_user_id"), table_name="audit_logs")
    op.drop_index(op.f("ix_audit_logs_event_type"), table_name="audit_logs")
    op.drop_table("audit_logs")
