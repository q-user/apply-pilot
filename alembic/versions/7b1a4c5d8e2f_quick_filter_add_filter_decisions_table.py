"""quick_filter: add filter_decisions table

Revision ID: 7b1a4c5d8e2f
Revises: 5a3c8f7e1b2d, b1d2e3f4a5b6, d4e7f8a9b123, d5f8a9b0c123, e20f9a1b2c3d
Create Date: 2026-06-15 23:00:00.000000

Adds the ``filter_decisions`` table that persists the in-memory
:class:`FilterDecision` value object from the quick-filter vertical
slice (issue #27) so the engine's verdicts are reviewable later.

Schema
------

* ``id`` — UUID primary key.
* ``search_profile_id`` — FK to ``search_profiles.id`` (issue #13),
  cascading on delete.
* ``vacancy_id`` — FK to ``vacancies.id`` (issue #23), cascading on
  delete.
* ``decision`` — ``"accept"`` or ``"reject"`` (the engine never
  surfaces ``"neutral"`` at the decision level; that verdict is only
  used by individual rules).
* ``reasons`` — JSON-encoded list of strings. ``Text`` is portable
  across sqlite (no native JSON column) and PostgreSQL.
* ``rule_version`` — integer tracking which version of the rule
  engine produced this verdict, so historical decisions can be
  re-interpreted when the rule set evolves.
* ``created_at`` — server-side timestamp with timezone.

The ``(search_profile_id, created_at)`` composite index accelerates
the "show me the most recent decisions for this profile" listing
query that powers the review UI.

This migration merges the five currently-open alembic heads into a
single linear chain by declaring all of them as ``down_revision``
sources. Once this revision lands, ``alembic upgrade head`` resolves
to a single target.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "7b1a4c5d8e2f"
# Merge the five currently-open heads into a single linear chain.
down_revision: str | Sequence[str] | None = (
    "5a3c8f7e1b2d",
    "b1d2e3f4a5b6",
    "d4e7f8a9b123",
    "d5f8a9b0c123",
    "e20f9a1b2c3d",
)
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "filter_decisions",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("search_profile_id", sa.String(length=36), nullable=False),
        sa.Column("vacancy_id", sa.String(length=36), nullable=False),
        sa.Column("decision", sa.String(length=20), nullable=False),
        sa.Column(
            "reasons",
            sa.Text(),
            nullable=False,
            server_default="[]",
        ),
        sa.Column(
            "rule_version",
            sa.Integer(),
            nullable=False,
            server_default="1",
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["search_profile_id"],
            ["search_profiles.id"],
            name=op.f("fk_filter_decisions_search_profile_id_search_profiles"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["vacancy_id"],
            ["vacancies.id"],
            name=op.f("fk_filter_decisions_vacancy_id_vacancies"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_filter_decisions")),
    )
    op.create_index(
        op.f("ix_filter_decisions_profile_created"),
        "filter_decisions",
        ["search_profile_id", "created_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_filter_decisions_profile_created"), table_name="filter_decisions")
    op.drop_table("filter_decisions")
