"""merge apply_rate_limit_events into the post-M8 main line

Revision ID: 5b7c8d9e0f1a
Revises: 4f389cbeb844, 9f1a2b3c4d5e
Create Date: 2026-06-25 12:00:00.000000

M10 (issue #204) dropped the ``f1a2b3c4d5e_hh_add_hh_resume_links_table``
migration. That branch was previously merged into the post-M7/M8 line
via the ``eb6c1c51520c, f1a2b3c4d5e6`` 2-parent ``down_revision`` set on
``a5b6c7d8e9f0`` / ``71a2b3c4d5e6`` / ``c63d4a1b2e3f`` and the
``eb6c1c51520c`` parent on those revisions after the fact.

That cleanup leaves a *new* split: the ``9f1a2b3c4d5e`` apply rate-limit
revision (added in PR #111, M5 #46) was never merged into the
``40b1271d5be8`` post-M7/M8 line because it was added on top of the
``5c8a9b0d1e2f → 1d2e3f4a5b6c → 9f1a2b3c4d5e`` chain, which itself was
not a child of the M7/M8 merge point. The chain has been quietly open
ever since.

This revision is a no-op merge that closes the gap: it has both
``4f389cbeb844`` (the max-accounts head, current ``alembic upgrade
head`` target on the main line) and ``9f1a2b3c4d5e`` (the orphaned
apply-rate-limit head) as parents and produces no schema changes. The
next migration added on the main line will naturally sit on top of this
merge.
"""

from __future__ import annotations

from collections.abc import Sequence

# revision identifiers, used by Alembic.
revision: str = "5b7c8d9e0f1a"
down_revision: str | Sequence[str] | None = ("4f389cbeb844", "9f1a2b3c4d5e")
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """No-op merge — see module docstring."""


def downgrade() -> None:
    """No-op merge — see module docstring."""
