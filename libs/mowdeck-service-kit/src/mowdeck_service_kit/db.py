"""Async SQLAlchemy helpers for PostgreSQL (install the ``db`` extra).

Migrations (Alembic) are not part of the kit; the service template runs them as a separate step
before the service starts. Every account-scoped query filters by
:func:`~mowdeck_service_kit.tenancy.current_account_id`.
"""

from collections.abc import AsyncGenerator, Callable
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

from mowdeck_service_kit.health import ReadinessCheck
from mowdeck_service_kit.settings import ServiceSettings

_DRIVERLESS_SCHEMES = {"postgresql": "postgresql+asyncpg", "postgres": "postgresql+asyncpg"}


def async_database_url(url: str) -> str:
    """Rewrite ``postgresql://`` (or ``postgres://``) to ``postgresql+asyncpg://``."""
    scheme, sep, rest = url.partition("://")
    if not sep:
        msg = "database URL must look like postgresql://user:pass@host/db"
        raise ValueError(msg)
    return f"{_DRIVERLESS_SCHEMES.get(scheme, scheme)}://{rest}"


def create_engine(settings: ServiceSettings, **options: Any) -> AsyncEngine:
    """Create the async engine for ``settings.database_url`` (raises if it is not set).

    The pool holds ``database_pool_size`` connections and opens up to ``database_max_overflow``
    more under load; ``options`` override both (one-shot jobs pass ``pool_size=1``).
    """
    if settings.database_url is None:
        msg = "MOWDECK_DATABASE_URL (or MOWDECK_DATABASE_URL_FILE) is not set"
        raise ValueError(msg)
    url = async_database_url(settings.database_url.get_secret_value())
    defaults: dict[str, Any] = {
        "pool_pre_ping": True,
        "pool_size": settings.database_pool_size,
        "max_overflow": settings.database_max_overflow,
    }
    return create_async_engine(url, **{**defaults, **options})


def readiness_check_db(engine: AsyncEngine, *, timeout_seconds: float = 2.0) -> ReadinessCheck:
    """A readiness check named ``database`` that runs ``SELECT 1``.

    It runs on a connection of its own, opened for the check and closed right after it, never on
    the request pool: a pool whose connections are all busy serving requests is load, not a
    broken database, and must not make the service unready (ADR 0077). Each check briefly uses
    one connection of the service's budget (docs/ops/compose.md).
    """
    probe = create_async_engine(engine.url, poolclass=NullPool)

    async def check() -> None:
        async with probe.connect() as connection:
            await connection.execute(text("SELECT 1"))

    return ReadinessCheck(name="database", check=check, timeout_seconds=timeout_seconds)


def session_dependency(
    engine: AsyncEngine,
) -> Callable[[], AsyncGenerator[AsyncSession]]:
    """Return a FastAPI dependency yielding one :class:`AsyncSession` per request.

    The session commits when the request handler returns and rolls back on exceptions::

        get_session = session_dependency(engine)

        @router.get("/mowers")
        async def list_mowers(
            account_id: CurrentAccountId,
            session: Annotated[AsyncSession, Depends(get_session)],
        ): ...
    """
    factory = async_sessionmaker(engine, expire_on_commit=False)

    async def get_session() -> AsyncGenerator[AsyncSession]:
        async with factory() as session:
            try:
                yield session
            except Exception:
                await session.rollback()
                raise
            else:
                await session.commit()

    return get_session
