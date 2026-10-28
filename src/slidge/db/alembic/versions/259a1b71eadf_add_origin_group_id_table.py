"""Add origin group ID table

Revision ID: 259a1b71eadf
Revises: cef02a8b1451
Create Date: 2025-11-26 06:50:04.509013

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "259a1b71eadf"
down_revision: str | None = "cef02a8b1451"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "group_msg_origin",
        sa.Column("foreign_key", sa.Integer(), nullable=False),
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("legacy_id", sa.String(), nullable=False),
        sa.Column("xmpp_id", sa.String(), nullable=False),
        sa.ForeignKeyConstraint(
            ["foreign_key"],
            ["room.id"],
            name=op.f("fk_group_msg_origin_foreign_key_room"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_group_msg_origin")),
    )
    with op.batch_alter_table("group_msg_origin", schema=None) as batch_op:
        batch_op.create_index(
            "ix_group_msg_origin_legacy_id", ["legacy_id", "foreign_key"], unique=False
        )

    # ### end Alembic commands ###


def downgrade() -> None:
    with op.batch_alter_table("group_msg_origin", schema=None) as batch_op:
        batch_op.drop_index("ix_group_msg_origin_legacy_id")

    op.drop_table("group_msg_origin")
    # ### end Alembic commands ###
