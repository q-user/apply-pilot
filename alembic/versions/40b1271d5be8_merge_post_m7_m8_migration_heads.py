"""merge post-M7-M8 migration heads

Revision ID: 40b1271d5be8
Revises: 71a2b3c4d5e6, a5b6c7d8e9f0, c63d4a1b2e3f, g7h8i9j0k1l2
Create Date: 2026-06-17 14:12:36.370378

"""

from __future__ import annotations

from collections.abc import Sequence

# revision identifiers, used by Alembic.
revision: str = "40b1271d5be8"
down_revision: str | None = ("71a2b3c4d5e6", "a5b6c7d8e9f0", "c63d4a1b2e3f", "g7h8i9j0k1l2")
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
