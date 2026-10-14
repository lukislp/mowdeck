"""In-memory store (tests and local experiments). Same semantics as the PostgreSQL store:
unique rules raise ``ConflictError``, transactions are all or nothing (a snapshot is restored on
error) and run one at a time."""

import asyncio
import copy
import dataclasses
import itertools
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from contextlib import asynccontextmanager
from typing import Any

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
    Site,
)


def _matches(value: Any, condition: Any) -> bool:
    match condition:
        case Gt(v):
            return value is not None and value > v
        case Lt(v):
            return value is not None and value < v
        case In(values):
            return value in values
        case _:
            return bool(value == condition)


class MemoryTable[T]:
    """Rows of one entity, keyed by primary key. ``unique`` lists attribute tuples (properties
    allowed); a tuple containing None is not checked, like a NULL in a unique index."""

    def __init__(
        self,
        pk: str,
        unique: Sequence[tuple[str, ...]] = (),
        id_factory: Callable[[], Any] | None = None,
    ) -> None:
        self.pk = pk
        self.unique = list(unique)
        self.rows: dict[Any, T] = {}
        self._id_factory = id_factory

    def _key(self, item: T) -> Any:
        return getattr(item, self.pk)

    def _check_unique(self, item: T, ignore: Any = None) -> None:
        for columns in self.unique:
            values = tuple(getattr(item, c) for c in columns)
            if any(v is None for v in values):
                continue
            for key, row in self.rows.items():
                if key != ignore and tuple(getattr(row, c) for c in columns) == values:
                    msg = f"{'/'.join(columns)} already exists"
                    raise ConflictError(msg)

    async def add(self, item: T) -> T:
        if self._id_factory is not None and self._key(item) is None:
            setattr(item, self.pk, self._id_factory())
        key = self._key(item)
        if key in self.rows:
            msg = f"{self.pk} already exists"
            raise ConflictError(msg)
        self._check_unique(item)
        self.rows[key] = copy.deepcopy(item)
        return item

    def _select(self, where: dict[str, Any]) -> list[T]:
        return [
            row
            for row in self.rows.values()
            if all(_matches(getattr(row, column), condition) for column, condition in where.items())
        ]

    async def get(self, **where: Any) -> T | None:
        found = self._select(where)
        if len(found) > 1:
            msg = f"several rows match {where}"
            raise LookupError(msg)
        return copy.deepcopy(found[0]) if found else None

    async def find(
        self,
        *,
        order_by: str | None = None,
        descending: bool = False,
        limit: int | None = None,
        **where: Any,
    ) -> list[T]:
        found = self._select(where)
        if order_by is not None:
            found.sort(key=lambda row: str(getattr(row, order_by)), reverse=descending)
        if limit is not None:
            found = found[:limit]
        return [copy.deepcopy(row) for row in found]

    async def update(self, item: T) -> None:
        key = self._key(item)
        if key not in self.rows:
            msg = f"no row with {self.pk}={key}"
            raise LookupError(msg)
        self._check_unique(item, ignore=key)
        self.rows[key] = copy.deepcopy(item)

    async def update_where(self, values: Mapping[str, Any], **where: Any) -> int:
        found = self._select(where)
        for row in found:
            for name, value in values.items():
                setattr(row, name, value)
        return len(found)

    async def delete(self, **where: Any) -> int:
        doomed = [self._key(row) for row in self._select(where)]
        for key in doomed:
            del self.rows[key]
        return len(doomed)

    async def count(self, **where: Any) -> int:
        return len(self._select(where))


@dataclasses.dataclass
class MemoryTables:
    """All tables of the memory store."""

    sites: MemoryTable[Site]
    groups: MemoryTable[Group]
    mowers: MemoryTable[Mower]
    mower_groups: MemoryTable[MowerGroup]
    area_names: MemoryTable[MowerAreaName]
    commands: MemoryTable[CommandRecord]
    audit: MemoryTable[AuditEntry]
    drive_sessions: MemoryTable[DriveSession]
    linked_accounts: MemoryTable[LinkedAccount]


class MemoryTx:
    """``Tx`` over the memory tables."""

    def __init__(self, tables: MemoryTables) -> None:
        self._tables = tables

    @property
    def sites(self) -> MemoryTable[Site]:
        return self._tables.sites

    @property
    def groups(self) -> MemoryTable[Group]:
        return self._tables.groups

    @property
    def mowers(self) -> MemoryTable[Mower]:
        return self._tables.mowers

    @property
    def mower_groups(self) -> MemoryTable[MowerGroup]:
        return self._tables.mower_groups

    @property
    def area_names(self) -> MemoryTable[MowerAreaName]:
        return self._tables.area_names

    @property
    def commands(self) -> MemoryTable[CommandRecord]:
        return self._tables.commands

    @property
    def audit(self) -> MemoryTable[AuditEntry]:
        return self._tables.audit

    @property
    def drive_sessions(self) -> MemoryTable[DriveSession]:
        return self._tables.drive_sessions

    @property
    def linked_accounts(self) -> MemoryTable[LinkedAccount]:
        return self._tables.linked_accounts

    async def try_lock(self, key: int) -> bool:
        """Transactions run one at a time, so the lock is always free."""
        del key
        return True


class MemoryStore:
    """Implements ``Store`` in memory."""

    def __init__(self) -> None:
        audit_ids = itertools.count(1)
        self.tables = MemoryTables(
            sites=MemoryTable("id", [("default_key",), ("account_id", "name_key")]),
            groups=MemoryTable("id", [("account_id", "name_key")]),
            mowers=MemoryTable("id", [("account_id", "serial"), ("iot_id",)]),
            mower_groups=MemoryTable("id", [("pair_key",)]),
            area_names=MemoryTable("pair_key"),
            commands=MemoryTable("command_id"),
            audit=MemoryTable("id", id_factory=lambda: next(audit_ids)),
            drive_sessions=MemoryTable("id"),
            linked_accounts=MemoryTable("id", [("account_id", "email_key")]),
        )
        self._lock = asyncio.Lock()
        self.transactions = 0

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[MemoryTx]:
        """Serialised transaction with rollback by snapshot."""
        async with self._lock:
            self.transactions += 1
            snapshot = {
                field.name: copy.deepcopy(getattr(self.tables, field.name).rows)
                for field in dataclasses.fields(self.tables)
            }
            try:
                yield MemoryTx(self.tables)
            except BaseException:
                for name, rows in snapshot.items():
                    getattr(self.tables, name).rows = rows
                raise
