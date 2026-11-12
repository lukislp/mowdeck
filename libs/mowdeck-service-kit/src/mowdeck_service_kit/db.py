# SPDX-FileCopyrightText: 2026 Lukas Körber
# SPDX-License-Identifier: Apache-2.0

"""Async SQLAlchemy helpers for PostgreSQL (install the ``db`` extra).

Migrations (Alembic) are not part of the kit; the service template runs them as a separate step
before the service starts. Every account-scoped query filters by
:func:`~mowdeck_service_kit.tenancy.current_account_id`.
"""

import weakref
from collections.abc import AsyncGenerator, Callable, Iterable
from typing import Any
from urllib.parse import parse_qsl, urlsplit
from uuid import UUID

from sqlalchemy import (
    Column,
    MetaData,
    String,
    Table,
    delete,
    func,
    literal,
    select,
    text,
    update,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import (
    AsyncConnection,
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

from mowdeck_service_kit.health import ReadinessCheck
from mowdeck_service_kit.settings import ServiceSettings
from mowdeck_service_kit.tls import client_context
from mowdeck_service_kit.user_erasure import REF_NAME, REF_SUBJECT, is_ref_name

_DRIVERLESS_SCHEMES = {"postgresql": "postgresql+asyncpg", "postgres": "postgresql+asyncpg"}


def async_database_url(url: str) -> str:
    """Rewrite ``postgresql://`` (or ``postgres://``) to ``postgresql+asyncpg://``."""
    scheme, sep, rest = url.partition("://")
    if not sep:
        msg = "database URL must look like postgresql://user:pass@host/db"
        raise ValueError(msg)
    return f"{_DRIVERLESS_SCHEMES.get(scheme, scheme)}://{rest}"


_SSL_QUERY_KEYS = ("ssl", "sslmode")

_connect_args: "weakref.WeakKeyDictionary[AsyncEngine, dict[str, Any]]" = (
    weakref.WeakKeyDictionary()
)
"""The connect arguments of every engine made by :func:`create_engine`, for its readiness probe."""


def database_connect_args(settings: ServiceSettings, url: str) -> dict[str, Any]:
    """asyncpg connect arguments: TLS verified against ``tls_ca_file`` when it is set.

    A URL that chooses TLS itself (``?ssl=...`` or ``?sslmode=...``, e.g. an external database
    with a public certificate) keeps its choice.
    """
    if settings.tls_ca_file is None:
        return {}
    query = urlsplit(url).query
    if any(key in _SSL_QUERY_KEYS for key, _ in parse_qsl(query, keep_blank_values=True)):
        return {}
    return {"ssl": client_context(settings.tls_ca_file)}


def create_engine(settings: ServiceSettings, **options: Any) -> AsyncEngine:
    """Create the async engine for ``settings.database_url`` (raises if it is not set).

    The pool holds ``database_pool_size`` connections and opens up to ``database_max_overflow``
    more under load; ``options`` override both (one-shot jobs pass ``pool_size=1``). With
    ``tls_ca_file`` the connections use TLS and verify the server against that CA.
    """
    if settings.database_url is None:
        msg = "MOWDECK_DATABASE_URL (or MOWDECK_DATABASE_URL_FILE) is not set"
        raise ValueError(msg)
    url = async_database_url(settings.database_url.get_secret_value())
    connect_args = {
        **database_connect_args(settings, url),
        **options.pop("connect_args", {}),
    }
    defaults: dict[str, Any] = {
        "pool_pre_ping": True,
        "pool_size": settings.database_pool_size,
        "max_overflow": settings.database_max_overflow,
    }
    engine = create_async_engine(url, connect_args=connect_args, **{**defaults, **options})
    _connect_args[engine] = connect_args
    return engine


def readiness_check_db(engine: AsyncEngine, *, timeout_seconds: float = 2.0) -> ReadinessCheck:
    """A readiness check named ``database`` that runs ``SELECT 1``.

    It runs on a connection of its own, opened for the check and closed right after it, never on
    the request pool: a pool whose connections are all busy serving requests is load, not a
    broken database, and must not make the service unready (ADR 0077). Each check briefly uses
    one connection of the service's budget (docs/ops/compose.md).
    """
    probe = create_async_engine(
        engine.url, poolclass=NullPool, connect_args=_connect_args.get(engine, {})
    )

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


ACCOUNT_COLUMN = "account_id"
"""The column that names the account a row belongs to, in every service's schema."""


def account_tables(metadata: MetaData) -> tuple[Table, ...]:
    """The tables of ``metadata`` with an :data:`ACCOUNT_COLUMN`, in dependency order.

    Read from the schema, not from a hand-kept list: a table added later is included without
    anybody remembering it (the account erasure, ADR 0082, counts what is left with it)."""
    return tuple(table for table in metadata.sorted_tables if ACCOUNT_COLUMN in table.c)


async def account_row_counts(
    conn: AsyncConnection, tables: Iterable[Table], account_id: UUID
) -> dict[str, int]:
    """The rows of ``account_id`` in each of ``tables`` (every one needs an
    :data:`ACCOUNT_COLUMN`), by table name; tables without such a row are left out."""
    counts: dict[str, int] = {}
    for table in tables:
        statement = (
            select(func.count()).select_from(table).where(table.c[ACCOUNT_COLUMN] == account_id)
        )
        found = int((await conn.execute(statement)).scalar_one())
        if found:
            counts[table.name] = found
    return counts


USER_COLUMN = "user_id"
"""The column that names the user a row belongs to (the user's own rows)."""


def user_tables(metadata: MetaData) -> tuple[Table, ...]:
    """The tables of ``metadata`` with a :data:`USER_COLUMN`: rows that are one user's own and
    go when the user is deleted (ADR 0082). Read from the schema, in dependency order."""
    return tuple(table for table in metadata.sorted_tables if USER_COLUMN in table.c)


async def delete_user_rows(
    conn: AsyncConnection,
    tables: Iterable[Table],
    user_id: UUID,
    account_id: UUID | None = None,
) -> int:
    """Delete the rows of ``user_id`` in each of ``tables`` (every one needs a
    :data:`USER_COLUMN`), children before parents; with ``account_id`` only that account's rows
    (tables with an :data:`ACCOUNT_COLUMN`). Idempotent; returns how many rows went."""
    deleted = 0
    for table in reversed(tuple(tables)):
        condition = table.c[USER_COLUMN] == user_id
        if account_id is not None and ACCOUNT_COLUMN in table.c:
            condition &= table.c[ACCOUNT_COLUMN] == account_id
        deleted += int((await conn.execute(delete(table).where(condition))).rowcount)
    return deleted


async def user_row_counts(
    conn: AsyncConnection, tables: Iterable[Table], user_id: UUID
) -> dict[str, int]:
    """The rows of ``user_id`` in each of ``tables`` (every one needs a :data:`USER_COLUMN`), by
    table name; tables without such a row are left out."""
    counts: dict[str, int] = {}
    for table in tables:
        statement = select(func.count()).select_from(table).where(table.c[USER_COLUMN] == user_id)
        found = int((await conn.execute(statement)).scalar_one())
        if found:
            counts[table.name] = found
    return counts


def is_principal_ref_column(column: Column[Any]) -> bool:
    """Whether a column stores a ``PrincipalRef`` as JSON. The convention of every schema: a
    JSONB column named ``owner`` or ``<verb>_by`` (``issued_by``, ``started_by``). A plain id of
    the same name (``created_by`` as a UUID) is opaque and not a reference with a name."""
    return isinstance(column.type, JSONB) and is_ref_name(column.name)


def principal_ref_columns(metadata: MetaData) -> tuple[Column[Any], ...]:
    """Every column of ``metadata`` that stores a ``PrincipalRef`` (so a person's name), read
    from the schema: a column added later is scrubbed and counted without anybody listing it."""
    return tuple(
        column
        for table in metadata.sorted_tables
        for column in table.columns
        if is_principal_ref_column(column)
    )


def _names(column: Column[Any], user_id: UUID) -> Any:
    """The rows whose reference in ``column`` is ``user_id`` and still carries a name."""
    return (column[REF_SUBJECT].astext == str(user_id)) & column[REF_NAME].astext.is_not(None)


async def scrub_principal_refs(
    conn: AsyncConnection,
    columns: Iterable[Column[Any]],
    user_id: UUID,
    account_id: UUID | None = None,
) -> int:
    """Remove the name of ``user_id`` from the references in ``columns`` and keep the rows
    (ADR 0082): the ``display_name`` key goes, the subject id and everything else stay. With
    ``account_id`` only in that account's rows (tables with an :data:`ACCOUNT_COLUMN`).
    Idempotent; returns how many references changed."""
    changed = 0
    for column in columns:
        table = column.table
        condition = _names(column, user_id)
        if account_id is not None and ACCOUNT_COLUMN in table.c:
            condition &= table.c[ACCOUNT_COLUMN] == account_id
        without_name = column.op("-", return_type=JSONB)(literal(REF_NAME, String))
        result = await conn.execute(
            update(table).where(condition).values({column.name: without_name})
        )
        changed += int(result.rowcount)
    return changed


async def named_principal_refs(
    conn: AsyncConnection, columns: Iterable[Column[Any]], user_id: UUID
) -> dict[str, int]:
    """The references in ``columns`` that still carry the name of ``user_id``, by
    ``table.column``; places without one are left out."""
    counts: dict[str, int] = {}
    for column in columns:
        statement = select(func.count()).select_from(column.table).where(_names(column, user_id))
        found = int((await conn.execute(statement)).scalar_one())
        if found:
            counts[f"{column.table.name}.{column.name}"] = found
    return counts
