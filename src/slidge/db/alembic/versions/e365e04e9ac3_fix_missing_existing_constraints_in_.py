"""Fix missing existing constraints in SQLite

Revision ID: e365e04e9ac3
Revises: abea9b63fa34
Create Date: 2026-02-11 19:57:06.448033

"""

import logging
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "e365e04e9ac3"
down_revision: str | None = "abea9b63fa34"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    fix_constraints("contact", "user_account_id", "jid", "legacy_id")
    fix_constraints("contact_sent", "contact_id", "msg_id")
    fix_constraints("mam", "room_id", "stanza_id")
    fix_constraints("attachment", "user_account_id", "legacy_file_id")
    fix_constraints("participant", "room_id", "resource", "contact_id", "occupant_id")


def fix_constraints(table: str, col1: str, *cols: str) -> None:
    bogus_name = f"uq_{table}_{col1}"

    inspector = sa.inspect(op.get_bind())
    constraints = inspector.get_unique_constraints(table)
    constraint_exists = any(
        constraint["name"] == bogus_name for constraint in constraints
    )
    if not constraint_exists:
        # If this runs on a newly created DB, this is unnecessary because we actually
        # edited past migration to allow running with postgresql.
        logging.info(f"No need to fix constraints for {bogus_name}")
        return

    with op.batch_alter_table(table, schema=None) as batch_op:
        logging.info(f"Dropping constraint {bogus_name}")
        batch_op.drop_constraint(bogus_name, type_="unique")
        for col2 in cols:
            op.execute(f"""
                DELETE FROM '{table}'
                WHERE id NOT IN (
                    SELECT MIN(id)
                    FROM '{table}'
                    GROUP BY '{col1}', '{col2}'
                );
            """)
            proper_name = f"uq_{table}_{col1}_{col2}"
            logging.info(f"Creating constraint {proper_name}")
            batch_op.create_unique_constraint(proper_name, [col1, col2])


def downgrade() -> None:
    raise RuntimeError("Downgrade not supported!")
