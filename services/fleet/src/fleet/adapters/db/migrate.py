"""Run the Alembic migrations (``upgrade head``) as a separate step before the service starts.

Container: ``python -m fleet.adapters.db.migrate``. The Alembic configuration is
built in code, so the read-only image needs no ``alembic.ini``; the ``alembic.ini`` next to the
service README is only for creating revisions during development.
"""

from pathlib import Path

from alembic import command
from alembic.config import Config

MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"


def alembic_config() -> Config:
    """Alembic configuration pointing at the packaged migrations."""
    config = Config()
    config.set_main_option("script_location", str(MIGRATIONS_DIR))
    return config


def main() -> None:
    """Upgrade the database named by ``MOWDECK_DATABASE_URL`` to the newest revision."""
    command.upgrade(alembic_config(), "head")


if __name__ == "__main__":
    main()
