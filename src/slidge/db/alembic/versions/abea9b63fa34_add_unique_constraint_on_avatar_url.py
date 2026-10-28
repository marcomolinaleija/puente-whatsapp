"""Add unique constraint on avatar URL

Revision ID: abea9b63fa34
Revises: 805fdc0bf984
Create Date: 2026-02-09 20:20:53.576218

"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "abea9b63fa34"
down_revision: str | None = "805fdc0bf984"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("""
        DELETE FROM avatar
        WHERE id NOT IN (
            SELECT MIN(id)
            FROM avatar
            GROUP BY url
        );
    """)

    with op.batch_alter_table("avatar", schema=None) as batch_op:
        batch_op.create_unique_constraint(batch_op.f("uq_avatar_url"), ["url"])


def downgrade() -> None:
    with op.batch_alter_table("avatar", schema=None) as batch_op:
        batch_op.drop_constraint(batch_op.f("uq_avatar_url"), type_="unique")
