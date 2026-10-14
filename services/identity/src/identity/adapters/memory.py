"""In-memory store (tests and local experiments). Same semantics as the PostgreSQL store:
unique constraints raise ``ConflictError``, transactions are all or nothing (a snapshot is
restored on error) and run one at a time."""

import asyncio
import copy
import dataclasses
import itertools
from collections.abc import AsyncIterator, Callable, Sequence
from contextlib import asynccontextmanager
from typing import Any

from identity.application.ports import Ge, Gt, In, Lt
from identity.domain.errors import ConflictError
from identity.domain.models import (
    Account,
    ApiToken,
    AuditEntry,
    CatalogueEntry,
    LoginAttempt,
    Membership,
    Role,
    RoleAssignment,
    Session,
    SigningKeyRecord,
    User,
)


def _matches(value: Any, condition: Any) -> bool:
    match condition:
        case Gt(v):
            return value is not None and value > v
        case Ge(v):
            return value is not None and value >= v
        case Lt(v):
            return value is not None and value < v
        case In(values):
            return value in values
        case _:
            return bool(value == condition)


class MemoryTable[T]:
    """Rows of one entity, keyed by primary key."""

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
            found.sort(key=lambda row: getattr(row, order_by), reverse=descending)
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

    async def delete(self, **where: Any) -> int:
        doomed = [self._key(row) for row in self._select(where)]
        for key in doomed:
            del self.rows[key]
        return len(doomed)

    async def count(self, **where: Any) -> int:
        return len(self._select(where))


@dataclasses.dataclass
class MemoryTx:
    """All tables of the memory store."""

    accounts: MemoryTable[Account]
    users: MemoryTable[User]
    memberships: MemoryTable[Membership]
    roles: MemoryTable[Role]
    assignments: MemoryTable[RoleAssignment]
    sessions: MemoryTable[Session]
    api_tokens: MemoryTable[ApiToken]
    signing_keys: MemoryTable[SigningKeyRecord]
    audit: MemoryTable[AuditEntry]
    login_attempts: MemoryTable[LoginAttempt]
    catalogue: MemoryTable[CatalogueEntry]


class MemoryStore:
    """Implements ``Store`` in memory."""

    def __init__(self) -> None:
        audit_ids = itertools.count(1)
        self.tables = MemoryTx(
            accounts=MemoryTable("id", [("slug",)]),
            users=MemoryTable("id", [("username_norm",), ("email_norm",)]),
            memberships=MemoryTable("id", [("account_id", "user_id")]),
            roles=MemoryTable("id", [("account_id", "key")]),
            assignments=MemoryTable(
                "id", [("subject_type", "subject_id", "role_id", "level_type", "level_id")]
            ),
            sessions=MemoryTable("id", [("refresh_hash",)]),
            api_tokens=MemoryTable("id"),
            signing_keys=MemoryTable("kid"),
            audit=MemoryTable("id", id_factory=lambda: next(audit_ids)),
            login_attempts=MemoryTable("id"),
            catalogue=MemoryTable("key"),
        )
        self._lock = asyncio.Lock()
        self.transactions = 0

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[MemoryTx]:
        """Serialised transaction with rollback by snapshot."""
        async with self._lock:
            self.transactions += 1
            snapshot = {
                field.name: dict(getattr(self.tables, field.name).rows)
                for field in dataclasses.fields(self.tables)
            }
            try:
                yield self.tables
            except BaseException:
                for name, rows in snapshot.items():
                    getattr(self.tables, name).rows = rows
                raise
