"""users: add is_admin flag

Revision ID: a1c2d3e4f5b6
Revises: 40b1271d5be8
Create Date: 2026-06-19 04:30:00.000000

Hand-written to mirror :class:`apply_pilot.features.users.models.User`.

We deliberately do not autogenerate: the dev DB is sqlite, the
production target is Postgres, and Alembic's autogenerate against
sqlite would also surface unrelated drift (the ``GUID`` type is
represented as ``CHAR(36)`` in the migrations but as the custom
``GUID`` decorator on the ORM side). The hand-picked ``Boolean`` +
``server_default=sa.text("false")`` matches the production target
verbatim and is the only change this revision introduces.

Public surface: the new ``is_admin`` column on ``users`` defaults to
``False`` for every existing row. There is no bootstrap path that
flips it to ``True`` automatically — operators must promote the first
admin via the new ``apply-pilot promote --email <email>`` CLI (see
issue #171).
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a1c2d3e4f5b6"
down_revision: str | None = "40b1271d5be8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add the ``is_admin`` boolean column to ``users`` (default False)."""
    op.add_column(
        "users",
        sa.Column(
            "is_admin",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )


def downgrade() -> None:
    """Drop the ``is_admin`` column from ``users``."""
    op.drop_column("users", "is_admin")
