"""PostgreSQL store: the generic ``Table`` port on SQLAlchemy Core (async, asyncpg).

One ``AsyncConnection`` per transaction (``engine.begin()``). Every insert runs in a savepoint,
so a unique violation becomes ``ConflictError`` without poisoning the transaction.
"""

import dataclasses
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from enum import Enum
from typing import Any

from sqlalchemy import ColumnElement, Table, delete, func, insert, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from identity.adapters.db import tables
from identity.application.ports import Ge, Gt, In, Lt
from identity.domain.errors import ConflictError
from identity.domain.models import (
    Account,
    ApiToken,
    AppLoginCode,
    AppLoginRequest,
    AuditEntry,
    CatalogueEntry,
    ExternalIdentity,
    LoginAttempt,
    Membership,
    OAuthState,
    Passkey,
    Role,
    RoleAssignment,
    Session,
    SigningKeyRecord,
    User,
    WebAuthnChallenge,
)


def _plain(value: Any) -> Any:
    return value.value if isinstance(value, Enum) else value


class SqlTable[T]:
    """``Table`` port for one entity class on one SQL table."""

    def __init__(
        self,
        conn: AsyncConnection,
        table: Table,
        entity: type[T],
        pk: str,
        *,
        autoincrement: bool = False,
    ) -> None:
        self.conn = conn
        self.table = table
        self.entity = entity
        self.pk = pk
        self.autoincrement = autoincrement
        self.fields = [f.name for f in dataclasses.fields(entity)]  # type: ignore[arg-type]

    def _values(self, item: T) -> dict[str, Any]:
        return {name: _plain(getattr(item, name)) for name in self.fields}

    def _where(self, where: dict[str, Any]) -> list[ColumnElement[bool]]:
        clauses: list[ColumnElement[bool]] = []
        for name, condition in where.items():
            column = self.table.c[name]
            match condition:
                case None:
                    clauses.append(column.is_(None))
                case Gt(v):
                    clauses.append(column > _plain(v))
                case Ge(v):
                    clauses.append(column >= _plain(v))
                case Lt(v):
                    clauses.append(column < _plain(v))
                case In(values):
                    clauses.append(column.in_([_plain(v) for v in values]))
                case _:
                    clauses.append(column == _plain(condition))
        return clauses

    def _entity(self, row: Any) -> T:
        return self.entity(**dict(row._mapping))

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

    accounts: SqlTable[Account]
    users: SqlTable[User]
    memberships: SqlTable[Membership]
    roles: SqlTable[Role]
    assignments: SqlTable[RoleAssignment]
    sessions: SqlTable[Session]
    api_tokens: SqlTable[ApiToken]
    signing_keys: SqlTable[SigningKeyRecord]
    audit: SqlTable[AuditEntry]
    login_attempts: SqlTable[LoginAttempt]
    catalogue: SqlTable[CatalogueEntry]
    passkeys: SqlTable[Passkey]
    webauthn_challenges: SqlTable[WebAuthnChallenge]
    app_login_codes: SqlTable[AppLoginCode]
    app_login_requests: SqlTable[AppLoginRequest]
    external_identities: SqlTable[ExternalIdentity]
    oauth_states: SqlTable[OAuthState]

    @classmethod
    def bind(cls, conn: AsyncConnection) -> "SqlTx":
        """Tables on ``conn``."""
        return cls(
            accounts=SqlTable(conn, tables.accounts, Account, "id"),
            users=SqlTable(conn, tables.users, User, "id"),
            memberships=SqlTable(conn, tables.memberships, Membership, "id"),
            roles=SqlTable(conn, tables.roles, Role, "id"),
            assignments=SqlTable(conn, tables.role_assignments, RoleAssignment, "id"),
            sessions=SqlTable(conn, tables.sessions, Session, "id"),
            api_tokens=SqlTable(conn, tables.api_tokens, ApiToken, "id"),
            signing_keys=SqlTable(conn, tables.signing_keys, SigningKeyRecord, "kid"),
            audit=SqlTable(conn, tables.audit_log, AuditEntry, "id", autoincrement=True),
            login_attempts=SqlTable(conn, tables.login_attempts, LoginAttempt, "id"),
            catalogue=SqlTable(conn, tables.permission_catalogue, CatalogueEntry, "key"),
            passkeys=SqlTable(conn, tables.passkeys, Passkey, "id"),
            webauthn_challenges=SqlTable(conn, tables.webauthn_challenges, WebAuthnChallenge, "id"),
            app_login_codes=SqlTable(conn, tables.app_login_codes, AppLoginCode, "id"),
            app_login_requests=SqlTable(conn, tables.app_login_requests, AppLoginRequest, "id"),
            external_identities=SqlTable(conn, tables.external_identities, ExternalIdentity, "id"),
            oauth_states=SqlTable(conn, tables.oauth_states, OAuthState, "id"),
        )


class SqlStore:
    """Implements ``Store`` on PostgreSQL."""

    def __init__(self, engine: AsyncEngine) -> None:
        self.engine = engine

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[SqlTx]:
        """``BEGIN`` ... ``COMMIT`` (``ROLLBACK`` on error)."""
        async with self.engine.begin() as conn:
            yield SqlTx.bind(conn)
