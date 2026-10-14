"""Alembic environment: runs migrations with the service settings (async engine)."""

import asyncio

from alembic import context
from mowdeck_service_kit.db import async_database_url, create_engine
from sqlalchemy.engine import Connection

from telemetry.adapters.db.tables import metadata
from telemetry.settings import Settings


def _configure(connection: Connection) -> None:
    context.configure(connection=connection, target_metadata=metadata)
    with context.begin_transaction():
        context.run_migrations()


async def _run_online() -> None:
    engine = create_engine(Settings.from_env(), pool_size=1, max_overflow=0)
    try:
        async with engine.connect() as connection:
            await connection.run_sync(_configure)
    finally:
        await engine.dispose()


def _run_offline() -> None:
    settings = Settings.from_env()
    if settings.database_url is None:
        msg = "MOWDECK_DATABASE_URL is not set"
        raise RuntimeError(msg)
    context.configure(
        url=async_database_url(settings.database_url.get_secret_value()),
        target_metadata=metadata,
        literal_binds=True,
    )
    with context.begin_transaction():
        context.run_migrations()


if context.is_offline_mode():
    _run_offline()
else:
    asyncio.run(_run_online())
