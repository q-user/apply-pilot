"""search_profiles: add is_preferred column

Revision ID: eb6c1c51520c
Revises: c4d5e6f7a8b9
Create Date: 2026-06-16 21:14:28.707419

Adds the ``is_preferred`` boolean column to the ``search_profiles`` table
as a data-model placeholder for a future M6 feature: a dedicated
``POST /search-profiles/{id}/preferred`` (or similar) endpoint that lets
the dashboard mark exactly one profile per user as "preferred". This
milestone (M6, issue #53) ships the GET side of that contract; the
follow-up issue will add the setter and the per-user uniqueness
constraint.

The column is non-null with a server-side default of ``0`` (sqlite) /
``false`` (postgres) so existing rows are back-filled safely.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "eb6c1c51520c"
down_revision: str | Sequence[str] | None = "c4d5e6f7a8b9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "search_profiles",
        sa.Column(
            "is_preferred",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )


def downgrade() -> None:
    op.drop_column("search_profiles", "is_preferred")
