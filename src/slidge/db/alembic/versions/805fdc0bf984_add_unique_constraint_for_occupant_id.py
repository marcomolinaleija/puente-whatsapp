"""Add unique constraint for occupant ID

Revision ID: 805fdc0bf984
Revises: b7a06a86416a
Create Date: 2025-12-17 19:55:11.058263

"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "805fdc0bf984"
down_revision: str | None = "b7a06a86416a"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("participant", schema=None) as batch_op:
        batch_op.create_unique_constraint(
            "uq_participant_room_id_occupant_id", ["room_id", "occupant_id"]
        )


def downgrade() -> None:
    with op.batch_alter_table("participant", schema=None) as batch_op:
        batch_op.drop_constraint(
            batch_op.f("uq_participant_room_id_occupant_id"), type_="unique"
        )
