"""matches denormalize user_id onto VacancyMatch.

Revision ID: c0d1e2f3a4b5
Revises: 9b1c2d3e4f56
Create Date: 2026-06-28 00:00:00.000000

"""

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "c0d1e2f3a4b5"
down_revision = "9b1c2d3e4f56"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Add the column nullable, backfill from search_profiles, then tighten.
    op.add_column(
        "vacancy_matches",
        sa.Column("user_id", sa.String(length=36), nullable=True),
    )
    op.execute(
        "UPDATE vacancy_matches AS vm "
        "SET user_id = ("
        "  SELECT sp.user_id FROM search_profiles AS sp "
        "  WHERE sp.id = vm.search_profile_id"
        ")"
    )
    op.alter_column("vacancy_matches", "user_id", nullable=False)
    op.create_index(
        op.f("ix_vacancy_matches_user_id"),
        "vacancy_matches",
        ["user_id"],
        unique=False,
    )
    op.create_foreign_key(
        op.f("fk_vacancy_matches_user_id_users"),
        "vacancy_matches",
        "users",
        ["user_id"],
        ["id"],
        ondelete="CASCADE",
    )


def downgrade() -> None:
    op.drop_constraint(
        op.f("fk_vacancy_matches_user_id_users"),
        "vacancy_matches",
        type_="foreignkey",
    )
    op.drop_index(op.f("ix_vacancy_matches_user_id"), table_name="vacancy_matches")
    op.drop_column("vacancy_matches", "user_id")
