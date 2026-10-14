"""PostgreSQL store: the generic ``Table`` port on SQLAlchemy Core (async, asyncpg).

One ``AsyncConnection`` per transaction (``engine.begin()``). Every insert and update runs in a
savepoint, so a unique violation becomes ``ConflictError`` without poisoning the transaction.
"""

import dataclasses
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from typing import Any

from sqlalchemy import ColumnElement, Table, delete, func, insert, select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from fleet.adapters.db import tables
from fleet.adapters.db.codecs import MOWER_CODECS, Codec
from fleet.application.ports import Gt, In, Lt
from fleet.domain.errors import ConflictError
from fleet.domain.models import (
    AuditEntry,
    CommandRecord,
    DriveSession,
    Group,
    LinkedAccount,
    Mower,
    MowerAreaName,
    MowerGroup,
    MowerLink,
    Site,
)


class SqlTable[T]:
    """``Table`` port for one entity class on one SQL table.

    ``codecs`` name the columns whose domain value is stored in another shape (JSON); they
    are applied to every value written, compared or read, so callers only see domain values.
    """

    def __init__(
        self,
        conn: AsyncConnection,
        table: Table,
        entity: type[T],
        pk: str,
        *,
        autoincrement: bool = False,
        codecs: Mapping[str, Codec] | None = None,
    ) -> None:
        self.conn = conn
        self.table = table
        self.entity = entity
        self.pk = pk
        self.autoincrement = autoincrement
        self.codecs = dict(codecs or {})
        self.fields = [f.name for f in dataclasses.fields(entity)]  # type: ignore[arg-type]

    def _encode(self, name: str, value: Any) -> Any:
        codec = self.codecs.get(name)
        return codec.encode(value) if codec is not None else value

    def _values(self, item: T) -> dict[str, Any]:
        return {name: self._encode(name, getattr(item, name)) for name in self.fields}

    def _column_values(self, values: Mapping[str, Any]) -> dict[str, Any]:
        return {name: self._encode(name, value) for name, value in values.items()}

    def _where(self, where: Mapping[str, Any]) -> list[ColumnElement[bool]]:
        clauses: list[ColumnElement[bool]] = []
        for name, condition in where.items():
            column = self.table.c[name]
            match condition:
                case None:
                    clauses.append(column.is_(None))
                case Gt(v):
                    clauses.append(column > v)
                case Lt(v):
                    clauses.append(column < v)
                case In(values):
                    clauses.append(column.in_(list(values)))
                case _:
                    clauses.append(column == self._encode(name, condition))
        return clauses

    def _entity(self, row: Any) -> T:
        data = dict(row._mapping)
        for name, codec in self.codecs.items():
            if name in data:
                data[name] = codec.decode(data[name])
        return self.entity(**data)

    async def add(self, item: T) -> T:
        values = self._values(item)
        if self.autoincrement and values.get(self.pk) is None:
            values.pop(self.pk)
        try:
            async with self.conn.begin_nested():
                result = await self.conn.execute(
                    insert(self.table).values(**values).returning(self.table.c[self.pk])
                )
        except IntegrityError as exc:
            msg = f"{self.table.name}: already exists"
            raise ConflictError(msg) from exc
        if self.autoincrement:
            setattr(item, self.pk, result.scalar_one())
        return item

    async def get(self, **where: Any) -> T | None:
        result = await self.conn.execute(select(self.table).where(*self._where(where)).limit(2))
        rows = result.all()
        if len(rows) > 1:
            msg = f"several {self.table.name} rows match"
            raise LookupError(msg)
        return self._entity(rows[0]) if rows else None

    async def find(
        self,
        *,
        order_by: str | None = None,
        descending: bool = False,
        limit: int | None = None,
        **where: Any,
    ) -> list[T]:
        statement = select(self.table).where(*self._where(where))
        if order_by is not None:
            column = self.table.c[order_by]
            statement = statement.order_by(column.desc() if descending else column.asc())
        if limit is not None:
            statement = statement.limit(limit)
        result = await self.conn.execute(statement)
        return [self._entity(row) for row in result.all()]

    async def update(self, item: T) -> None:
        values = self._values(item)
        key = values.pop(self.pk)
        try:
            async with self.conn.begin_nested():
                result = await self.conn.execute(
                    update(self.table).where(self.table.c[self.pk] == key).values(**values)
                )
        except IntegrityError as exc:
            msg = f"{self.table.name}: conflicting update"
            raise ConflictError(msg) from exc
        if result.rowcount != 1:
            msg = f"no {self.table.name} row with {self.pk}={key}"
            raise LookupError(msg)

    async def update_where(self, values: Mapping[str, Any], **where: Any) -> int:
        result = await self.conn.execute(
            update(self.table).where(*self._where(where)).values(**self._column_values(values))
        )
        return int(result.rowcount)

    async def delete(self, **where: Any) -> int:
        result = await self.conn.execute(delete(self.table).where(*self._where(where)))
        return int(result.rowcount)

    async def count(self, **where: Any) -> int:
        result = await self.conn.execute(
            select(func.count()).select_from(self.table).where(*self._where(where))
        )
        return int(result.scalar_one())


@dataclasses.dataclass
class SqlTx:
    """All tables bound to one connection."""

    conn: AsyncConnection
    sites: SqlTable[Site]
    groups: SqlTable[Group]
    mowers: SqlTable[Mower]
    mower_groups: SqlTable[MowerGroup]
    area_names: SqlTable[MowerAreaName]
    commands: SqlTable[CommandRecord]
    audit: SqlTable[AuditEntry]
    drive_sessions: SqlTable[DriveSession]
    linked_accounts: SqlTable[LinkedAccount]
    mower_links: SqlTable[MowerLink]

    @classmethod
    def bind(cls, conn: AsyncConnection) -> "SqlTx":
        """Tables on ``conn``."""
        return cls(
            conn=conn,
            sites=SqlTable(conn, tables.sites, Site, "id"),
            groups=SqlTable(conn, tables.groups, Group, "id"),
            mowers=SqlTable(conn, tables.mowers, Mower, "id", codecs=MOWER_CODECS),
            mower_groups=SqlTable(conn, tables.mower_groups, MowerGroup, "id"),
            # Composite key: change rows with update_where/delete, never update().
            area_names=SqlTable(conn, tables.mower_area_names, MowerAreaName, "mower_id"),
            commands=SqlTable(conn, tables.commands, CommandRecord, "command_id"),
            audit=SqlTable(conn, tables.audit_log, AuditEntry, "id", autoincrement=True),
            drive_sessions=SqlTable(conn, tables.drive_sessions, DriveSession, "id"),
            linked_accounts=SqlTable(conn, tables.linked_accounts, LinkedAccount, "id"),
            mower_links=SqlTable(conn, tables.mower_links, MowerLink, "id"),
        )

    async def try_lock(self, key: int) -> bool:
        """``pg_try_advisory_xact_lock``: held until the transaction ends."""
        result = await self.conn.execute(
            text("SELECT pg_try_advisory_xact_lock(:key)"), {"key": key}
        )
        return bool(result.scalar_one())


class SqlStore:
    """Implements ``Store`` on PostgreSQL."""

    def __init__(self, engine: AsyncEngine) -> None:
        self.engine = engine

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[SqlTx]:
        """``BEGIN`` ... ``COMMIT`` (``ROLLBACK`` on error)."""
        async with self.engine.begin() as conn:
            yield SqlTx.bind(conn)
