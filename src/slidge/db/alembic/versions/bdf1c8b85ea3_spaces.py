"""Spaces

Revision ID: bdf1c8b85ea3
Revises: e365e04e9ac3
Create Date: 2026-05-19 20:20:35.452386

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "bdf1c8b85ea3"
down_revision: str | None = "e365e04e9ac3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "space",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("updated", sa.Boolean(), nullable=False),
        sa.Column("user_account_id", sa.Integer(), nullable=False),
        sa.Column("legacy_id", sa.String(), nullable=False),
        sa.Column("name", sa.String(), nullable=True),
        sa.Column("description", sa.String(), nullable=True),
        sa.Column("member_count", sa.Integer(), nullable=True),
        sa.Column("creator_pk", sa.Integer(), nullable=True),
        sa.ForeignKeyConstraint(
            ["creator_pk"], ["contact.id"], name=op.f("fk_space_creator_pk_contact")
        ),
        sa.ForeignKeyConstraint(
            ["user_account_id"],
            ["user_account.id"],
            name=op.f("fk_space_user_account_id_user_account"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_space")),
        sa.UniqueConstraint(
            "user_account_id", "legacy_id", name="uq_space_user_account_id_legacy_id"
        ),
    )
    op.create_table(
        "space_owner_association",
        sa.Column("space_id", sa.Integer(), nullable=False),
        sa.Column("contact_id", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(
            ["contact_id"],
            ["contact.id"],
            name=op.f("fk_space_owner_association_contact_id_contact"),
        ),
        sa.ForeignKeyConstraint(
            ["space_id"],
            ["space.id"],
            name=op.f("fk_space_owner_association_space_id_space"),
        ),
        sa.PrimaryKeyConstraint(
            "space_id", "contact_id", name=op.f("pk_space_owner_association")
        ),
    )
    with op.batch_alter_table("room", schema=None) as batch_op:
        batch_op.add_column(sa.Column("space_id", sa.Integer(), nullable=True))
        batch_op.create_foreign_key(
            batch_op.f("fk_room_space_id_space"), "space", ["space_id"], ["id"]
        )


def downgrade() -> None:
    with op.batch_alter_table("room", schema=None) as batch_op:
        batch_op.drop_constraint(
            batch_op.f("fk_room_space_id_space"), type_="foreignkey"
        )
        batch_op.drop_column("space_id")

    op.drop_table("space_owner_association")
    op.drop_table("space")
