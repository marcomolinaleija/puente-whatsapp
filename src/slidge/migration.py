import logging
import sys
import traceback
from pathlib import Path

from alembic import command
from alembic.config import Config


def get_alembic_cfg() -> Config:
    alembic_cfg = Config()
    alembic_cfg.set_section_option(
        "alembic",
        "script_location",
        str(Path(__file__).parent / "db" / "alembic"),
    )
    return alembic_cfg


def migrate() -> None:
    try:
        command.upgrade(get_alembic_cfg(), "head")
    except Exception as e:
        traceback.print_exception(e)
        print(
            "Something went wrong during the migration. "
            "This is expected if you upgrade from slidge 0.2, in this case you need to start from a fresh database."
        )
        exit(1)


def main() -> None:
    """
    Updates the (dev) database in ./dev/slidge.sqlite and generates a revision

    Usage: python -m slidge.migration "Revision message blah blah blah"
    """
    dev_db = Path(".") / "dev" / "slidge.sqlite"
    if dev_db.exists():
        # always start from a clean state
        dev_db.unlink()
    alembic_cfg = get_alembic_cfg()
    command.upgrade(alembic_cfg, "head")
    command.revision(alembic_cfg, sys.argv[1], autogenerate=True)


log = logging.getLogger(__name__)

if __name__ == "__main__":
    main()
