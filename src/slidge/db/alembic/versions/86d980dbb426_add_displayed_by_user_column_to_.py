"""Add 'displayed_by_user' column to ArchivedMessage, for xmpp.MARK_ALL_MESSAGES

Revision ID: 86d980dbb426
Revises: 259a1b71eadf
Create Date: 2025-12-02 20:47:51.919688

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "86d980dbb426"
down_revision: str | None = "259a1b71eadf"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("mam", schema=None) as batch_op:
        batch_op.add_column(sa.Column("displayed_by_user", sa.Boolean()))
    # Set all existing rows to displayed, to avoid sending large busts of read markers
    # for messages archived before the introduction of the new column.
    mam = sa.table("mam", sa.column("displayed_by_user", sa.Boolean()))
    op.execute(mam.update().values(displayed_by_user=True))


def downgrade() -> None:
    with op.batch_alter_table("mam", schema=None) as batch_op:
        batch_op.drop_column("displayed_by_user")
