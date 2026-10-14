"""Run the Alembic migrations (``upgrade head``) as a separate step before the service starts.

Container: ``python -m entitlements.adapters.db.migrate``. The Alembic configuration is
built in code, so the read-only image needs no ``alembic.ini``; the ``alembic.ini`` next to the
service README is only for creating revisions during development.

A refused connection is retried for a while: on the very first start of the Compose stack the
PostgreSQL image reports healthy on its socket while its init scripts still run without TCP.
"""

import time
from collections.abc import Callable
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy.exc import OperationalError

MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"


def alembic_config() -> Config:
    """Alembic configuration pointing at the packaged migrations."""
    config = Config()
    config.set_main_option("script_location", str(MIGRATIONS_DIR))
    return config


def upgrade_with_retry(
    upgrade: Callable[[], None],
    *,
    attempts: int = 20,
    delay_seconds: float = 2.0,
    sleep: Callable[[float], None] = time.sleep,
) -> None:
    """Run ``upgrade``; retry while the database refuses connections."""
    for attempt in range(1, attempts + 1):
        try:
            upgrade()
        except (OSError, OperationalError):
            if attempt == attempts:
                raise
            sleep(delay_seconds)
        else:
            return


def main() -> None:
    """Upgrade the database named by ``MOWDECK_DATABASE_URL`` to the newest revision."""
    upgrade_with_retry(lambda: command.upgrade(alembic_config(), "head"))


if __name__ == "__main__":
    main()
