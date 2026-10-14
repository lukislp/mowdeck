"""PostgreSQL store on SQLAlchemy Core (async, asyncpg).

Snapshots are upserts that never replace a newer observation (``observed_at``); templates are
upserted by ``(account_id, name)``.
"""

import dataclasses
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Any
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from planner.adapters.db import tables
from planner.domain.models import MowerFacts, Template

_FACT_FIELDS = [f.name for f in dataclasses.fields(MowerFacts)]
_TEMPLATE_FIELDS = [f.name for f in dataclasses.fields(Template)]


def _facts(row: Any) -> MowerFacts:
    values = dict(row._mapping)
    return MowerFacts(**{name: values[name] for name in _FACT_FIELDS})


def _template(row: Any) -> Template:
    values = dict(row._mapping)
    return Template(**{name: values[name] for name in _TEMPLATE_FIELDS})


class SqlTx:
    """``Tx`` on one connection."""

    def __init__(self, conn: AsyncConnection) -> None:
        self._conn = conn

    async def get_facts(self, mower_id: UUID) -> MowerFacts | None:
        f = tables.mower_facts
        row = (await self._conn.execute(sa.select(f).where(f.c.mower_id == mower_id))).first()
        return _facts(row) if row else None

    async def _put(
        self,
        kind: str,
        mower_id: UUID,
        account_id: UUID,
        observed_at: datetime,
        value: dict[str, object],
    ) -> bool:
        f = tables.mower_facts
        stamp_name = f"{kind}_observed_at"
        stamp = f.c[stamp_name]
        values = {kind: value, stamp_name: observed_at, "account_id": account_id}
        statement = (
            pg_insert(f)
            .values({"mower_id": mower_id, **values})
            .on_conflict_do_update(
                index_elements=[f.c.mower_id],
                set_=values,
                where=sa.or_(stamp.is_(None), stamp <= observed_at),
            )
        )
        result = await self._conn.execute(statement)
        return bool(result.rowcount)

    async def put_map(
        self, mower_id: UUID, account_id: UUID, observed_at: datetime, value: dict[str, object]
    ) -> bool:
        return await self._put("map", mower_id, account_id, observed_at, value)

    async def put_capabilities(
        self, mower_id: UUID, account_id: UUID, observed_at: datetime, value: dict[str, object]
    ) -> bool:
        return await self._put("capabilities", mower_id, account_id, observed_at, value)

    async def list_templates(self, account_id: UUID) -> list[Template]:
        t = tables.templates
        result = await self._conn.execute(
            sa.select(t).where(t.c.account_id == account_id).order_by(t.c.name)
        )
        return [_template(row) for row in result]

    async def get_template(self, account_id: UUID, name: str) -> Template | None:
        t = tables.templates
        statement = sa.select(t).where(t.c.account_id == account_id, t.c.name == name)
        row = (await self._conn.execute(statement)).first()
        return _template(row) if row else None

    async def count_templates(self, account_id: UUID) -> int:
        t = tables.templates
        result = await self._conn.execute(
            sa.select(sa.func.count()).select_from(t).where(t.c.account_id == account_id)
        )
        return int(result.scalar_one())

    async def put_template(self, template: Template) -> None:
        t = tables.templates
        values = {name: getattr(template, name) for name in _TEMPLATE_FIELDS}
        statement = pg_insert(t).values(values)
        statement = statement.on_conflict_do_update(
            constraint="uq_templates_account_name",
            set_={
                "description": statement.excluded.description,
                "chain": statement.excluded.chain,
                "updated_at": statement.excluded.updated_at,
            },
        )
        await self._conn.execute(statement)

    async def delete_template(self, account_id: UUID, name: str) -> bool:
        t = tables.templates
        result = await self._conn.execute(
            sa.delete(t).where(t.c.account_id == account_id, t.c.name == name)
        )
        return bool(result.rowcount)


class SqlStore:
    """``Store`` on an async engine."""

    def __init__(self, engine: AsyncEngine) -> None:
        self.engine = engine

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[SqlTx]:
        """A transaction on a pooled connection."""
        async with self.engine.begin() as conn:
            yield SqlTx(conn)
