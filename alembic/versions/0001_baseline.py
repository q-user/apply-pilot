"""baseline — M0 schema root.

Revision ID: 0001_baseline
Revises:
Create Date: 2026-06-14

The M0 baseline is intentionally schema-empty: it anchors the Alembic
revision graph so subsequent feature slices (orders, etc.) can stack
their own migrations on top. Feature tables are NOT created here —
they ship with their own vertical-slice migrations (see issue #7 and
the PR body for context).
"""

from __future__ import annotations

from collections.abc import Sequence

# revision identifiers, used by Alembic.
revision: str = "0001_baseline"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """No-op baseline. Feature tables are added in their own slices."""


def downgrade() -> None:
    """No-op baseline. Feature tables are removed in their own slices."""
