"""PostgreSQL store on SQLAlchemy Core (async, asyncpg).

* The ticker claims one due schedule per transaction with ``FOR UPDATE SKIP LOCKED``: the row
  stays locked while it fires, so another replica skips it and takes the next one.
* Open runs (started, no follow-up yet) are claimed the same way.
* The latest activity of a mower is an upsert that never replaces a newer observation.
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

from scheduler.adapters.db import tables
from scheduler.domain.models import MowerPlace, MowerStatus, Schedule, ScheduleRun

_SCHEDULE_FIELDS = [f.name for f in dataclasses.fields(Schedule)]
_RUN_FIELDS = [f.name for f in dataclasses.fields(ScheduleRun)]


def _schedule(row: Any) -> Schedule:
    values = dict(row._mapping)
    return Schedule(**{name: values[name] for name in _SCHEDULE_FIELDS})


def _run(row: Any) -> ScheduleRun:
    values = dict(row._mapping)
    return ScheduleRun(**{name: values[name] for name in _RUN_FIELDS})


def _values(item: Any) -> dict[str, Any]:
    return {f.name: getattr(item, f.name) for f in dataclasses.fields(item)}


class SqlTx:
    """``Tx`` on one connection."""

    def __init__(self, conn: AsyncConnection) -> None:
        self._conn = conn

    async def add_schedule(self, schedule: Schedule) -> None:
        await self._conn.execute(sa.insert(tables.schedules).values(**_values(schedule)))

    async def get_schedule(
        self, schedule_id: UUID, account_id: UUID | None = None, *, for_update: bool = False
    ) -> Schedule | None:
        s = tables.schedules
        statement = sa.select(s).where(s.c.id == schedule_id)
        if account_id is not None:
            statement = statement.where(s.c.account_id == account_id)
        if for_update:
            statement = statement.with_for_update()
        row = (await self._conn.execute(statement)).first()
        return _schedule(row) if row else None

    async def list_schedules(self, account_id: UUID) -> list[Schedule]:
        s = tables.schedules
        result = await self._conn.execute(
            sa.select(s).where(s.c.account_id == account_id).order_by(s.c.name, s.c.id)
        )
        return [_schedule(row) for row in result]

    async def count_schedules(self, account_id: UUID) -> int:
        s = tables.schedules
        result = await self._conn.execute(
            sa.select(sa.func.count()).select_from(s).where(s.c.account_id == account_id)
        )
        return int(result.scalar_one())

    async def update_schedule(self, schedule: Schedule) -> None:
        values = _values(schedule)
        key = values.pop("id")
        s = tables.schedules
        await self._conn.execute(sa.update(s).where(s.c.id == key).values(**values))

    async def delete_schedule(self, schedule_id: UUID, account_id: UUID) -> bool:
        s = tables.schedules
        result = await self._conn.execute(
            sa.delete(s).where(s.c.id == schedule_id, s.c.account_id == account_id)
        )
        return bool(result.rowcount)

    async def claim_due(self, now: datetime) -> Schedule | None:
        s = tables.schedules
        row = (
            await self._conn.execute(
                sa.select(s)
                .where(s.c.enabled, s.c.next_run_at.is_not(None), s.c.next_run_at <= now)
                .order_by(s.c.next_run_at)
                .limit(1)
                .with_for_update(skip_locked=True)
            )
        ).first()
        return _schedule(row) if row else None

    async def add_run(self, run: ScheduleRun) -> None:
        await self._conn.execute(sa.insert(tables.schedule_runs).values(**_values(run)))

    async def update_run(self, run: ScheduleRun) -> None:
        values = _values(run)
        key = values.pop("id")
        r = tables.schedule_runs
        await self._conn.execute(sa.update(r).where(r.c.id == key).values(**values))

    async def list_runs(
        self, schedule_id: UUID, account_id: UUID, *, limit: int, before: datetime | None
    ) -> list[ScheduleRun]:
        r = tables.schedule_runs
        statement = sa.select(r).where(r.c.schedule_id == schedule_id, r.c.account_id == account_id)
        if before is not None:
            statement = statement.where(r.c.started_at < before)
        result = await self._conn.execute(
            statement.order_by(r.c.started_at.desc(), r.c.id).limit(limit)
        )
        return [_run(row) for row in result]

    async def claim_open_runs(self, limit: int) -> list[ScheduleRun]:
        r = tables.schedule_runs
        result = await self._conn.execute(
            sa.select(r)
            .where(r.c.outcome == "started", r.c.followup.is_(None))
            .order_by(r.c.started_at)
            .limit(limit)
            .with_for_update(skip_locked=True)
        )
        return [_run(row) for row in result]

    async def put_status(self, status: MowerStatus) -> bool:
        values = _values(status)
        insert = pg_insert(tables.mower_status).values(**values)
        result = await self._conn.execute(
            insert.on_conflict_do_update(
                index_elements=[tables.mower_status.c.mower_id],
                set_={k: insert.excluded[k] for k in values if k != "mower_id"},
                where=tables.mower_status.c.observed_at < insert.excluded.observed_at,
            )
        )
        return bool(result.rowcount)

    async def get_status(self, mower_id: UUID) -> MowerStatus | None:
        m = tables.mower_status
        row = (await self._conn.execute(sa.select(m).where(m.c.mower_id == mower_id))).first()
        return MowerStatus(**dict(row._mapping)) if row else None

    async def put_place(self, place: MowerPlace) -> None:
        values = _values(place)
        insert = pg_insert(tables.mower_places).values(**values)
        await self._conn.execute(
            insert.on_conflict_do_update(
                index_elements=[tables.mower_places.c.mower_id],
                set_={k: insert.excluded[k] for k in values if k != "mower_id"},
            )
        )

    async def get_place(self, mower_id: UUID) -> MowerPlace | None:
        p = tables.mower_places
        row = (await self._conn.execute(sa.select(p).where(p.c.mower_id == mower_id))).first()
        return MowerPlace(**dict(row._mapping)) if row else None

    async def purge_runs(self, before: datetime) -> int:
        r = tables.schedule_runs
        result = await self._conn.execute(
            sa.delete(r).where(
                r.c.started_at < before,
                sa.or_(r.c.outcome != "started", r.c.followup.is_not(None)),
            )
        )
        return int(result.rowcount)

    async def try_lock(self, key: int) -> bool:
        result = await self._conn.execute(
            sa.text("SELECT pg_try_advisory_xact_lock(:key)"), {"key": key}
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
            yield SqlTx(conn)
