"""PostgreSQL store on SQLAlchemy Core (async, asyncpg). Upserts use ``ON CONFLICT``."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any
from uuid import UUID

import sqlalchemy as sa
from mowdeck_contracts.v1 import PlanLimits
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from entitlements.adapters.db import tables
from entitlements.domain.models import AccountPlan, FeatureEntry, Plan, PlanHistory


def _plan(row: Any) -> Plan:
    values = dict(row._mapping)
    return Plan(
        key=values["key"],
        name=values["name"],
        description=values["description"],
        limits=PlanLimits.model_validate(values["limits"] or {}),
        features=list(values["features"] or []),
        trial_days=values["trial_days"],
        is_default=values["is_default"],
        active=values["active"],
        file_digest=values["file_digest"],
        created_at=values["created_at"],
        updated_at=values["updated_at"],
    )


def _plan_values(plan: Plan) -> dict[str, Any]:
    return {
        "key": plan.key,
        "name": plan.name,
        "description": plan.description,
        "limits": plan.limits_json(),
        "features": list(plan.features),
        "trial_days": plan.trial_days,
        "is_default": plan.is_default,
        "active": plan.active,
        "file_digest": plan.file_digest,
        "created_at": plan.created_at,
        "updated_at": plan.updated_at,
    }


def _account(row: Any) -> AccountPlan:
    values = dict(row._mapping)
    return AccountPlan(**values)


def _account_values(row: AccountPlan) -> dict[str, Any]:
    return {
        "account_id": row.account_id,
        "account_name": row.account_name,
        "plan_key": row.plan_key,
        "status": row.status.value,
        "since": row.since,
        "trial_until": row.trial_until,
        "grace_until": row.grace_until,
        "source": row.source.value,
        "updated_at": row.updated_at,
    }


class SqlTx:
    """``Tx`` on one connection."""

    def __init__(self, conn: AsyncConnection) -> None:
        self._conn = conn

    async def list_plans(self) -> list[Plan]:
        result = await self._conn.execute(sa.select(tables.plans).order_by(tables.plans.c.key))
        return [_plan(row) for row in result]

    async def get_plan(self, key: str) -> Plan | None:
        p = tables.plans
        row = (await self._conn.execute(sa.select(p).where(p.c.key == key))).first()
        return _plan(row) if row else None

    async def save_plan(self, plan: Plan) -> None:
        values = _plan_values(plan)
        statement = pg_insert(tables.plans).values(**values)
        update = {k: statement.excluded[k] for k in values if k not in ("key", "created_at")}
        await self._conn.execute(
            statement.on_conflict_do_update(index_elements=["key"], set_=update)
        )

    async def clear_default(self, except_key: str) -> None:
        p = tables.plans
        await self._conn.execute(
            sa.update(p).where(p.c.key != except_key, p.c.is_default).values(is_default=False)
        )

    async def get_account(self, account_id: UUID) -> AccountPlan | None:
        a = tables.account_plans
        row = (await self._conn.execute(sa.select(a).where(a.c.account_id == account_id))).first()
        return _account(row) if row else None

    async def save_account(self, row: AccountPlan) -> None:
        values = _account_values(row)
        statement = pg_insert(tables.account_plans).values(**values)
        update = {k: statement.excluded[k] for k in values if k != "account_id"}
        await self._conn.execute(
            statement.on_conflict_do_update(index_elements=["account_id"], set_=update)
        )

    async def add_account_if_absent(self, row: AccountPlan) -> bool:
        statement = (
            pg_insert(tables.account_plans)
            .values(**_account_values(row))
            .on_conflict_do_nothing(index_elements=["account_id"])
        )
        result = await self._conn.execute(statement)
        return bool(result.rowcount)

    async def list_accounts(self, offset: int, limit: int) -> list[AccountPlan]:
        a = tables.account_plans
        result = await self._conn.execute(
            sa.select(a).order_by(a.c.since, a.c.account_id).offset(offset).limit(limit)
        )
        return [_account(row) for row in result]

    async def count_accounts(self) -> int:
        result = await self._conn.execute(
            sa.select(sa.func.count()).select_from(tables.account_plans)
        )
        return int(result.scalar_one())

    async def add_history(self, entry: PlanHistory) -> None:
        await self._conn.execute(
            sa.insert(tables.plan_history).values(
                id=entry.id,
                account_id=entry.account_id,
                at=entry.at,
                plan_key=entry.plan_key,
                status=entry.status.value,
                source=entry.source.value,
                trial_until=entry.trial_until,
                grace_until=entry.grace_until,
                reason=entry.reason,
                actor=entry.actor,
            )
        )

    async def history(self, account_id: UUID, limit: int) -> list[PlanHistory]:
        h = tables.plan_history
        result = await self._conn.execute(
            sa.select(h)
            .where(h.c.account_id == account_id)
            .order_by(h.c.at.desc(), h.c.id)
            .limit(limit)
        )
        return [PlanHistory(**dict(row._mapping)) for row in result]

    async def list_features(self) -> list[FeatureEntry]:
        f = tables.features
        result = await self._conn.execute(sa.select(f).order_by(f.c.key))
        return [FeatureEntry(**dict(row._mapping)) for row in result]

    async def save_feature(self, entry: FeatureEntry) -> None:
        values = {
            "key": entry.key,
            "description": entry.description,
            "module": entry.module,
            "first_seen_at": entry.first_seen_at,
            "last_seen_at": entry.last_seen_at,
            "retired": entry.retired,
        }
        statement = pg_insert(tables.features).values(**values)
        update = {k: statement.excluded[k] for k in values if k not in ("key", "first_seen_at")}
        await self._conn.execute(
            statement.on_conflict_do_update(index_elements=["key"], set_=update)
        )


class SqlStore:
    """Implements ``Store`` on PostgreSQL."""

    def __init__(self, engine: AsyncEngine) -> None:
        self.engine = engine

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[SqlTx]:
        """``BEGIN`` ... ``COMMIT`` (``ROLLBACK`` on error)."""
        async with self.engine.begin() as conn:
            yield SqlTx(conn)
