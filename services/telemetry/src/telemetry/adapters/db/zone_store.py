"""PostgreSQL store of the virtual zones (SQLAlchemy Core, asyncpg; ADR 0070).

One mower's evaluations are serialised by a transaction level advisory lock on the mower (the
first evaluation has no row to lock yet) and its ``zone_presence`` row is read ``FOR UPDATE``,
so two replicas that consume states of the same mower never count the same interval twice. Day
totals are added with an upsert (``seconds = seconds + excluded.seconds``); a zone deleted in
the meantime is skipped.
"""

import dataclasses
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import date
from typing import Any
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from telemetry.adapters.db import tables
from telemetry.domain.zones import LatLon, Presence, VirtualZone, ZoneDay, ZoneTime

_Z = tables.virtual_zones.c
_P = tables.zone_presence.c
_D = tables.zone_days.c

_ZONE_LOCK = sa.text("SELECT pg_advisory_xact_lock(hashtext(:key))")


def _zone(row: Any) -> VirtualZone:
    values = dict(row._mapping)
    ring: tuple[LatLon, ...] = tuple((float(p[0]), float(p[1])) for p in values.pop("ring"))
    return VirtualZone(ring=ring, **values)


def _zone_values(zone: VirtualZone) -> dict[str, Any]:
    values = {f.name: getattr(zone, f.name) for f in dataclasses.fields(zone)}
    values["ring"] = [list(p) for p in zone.ring]
    return values


def _presence(row: Any) -> Presence:
    return Presence(**row._mapping)


class _SqlPresenceTx:
    def __init__(self, conn: AsyncConnection, mower_id: UUID) -> None:
        self._conn = conn
        self._mower_id = mower_id

    async def presence(self) -> Presence | None:
        row = (
            await self._conn.execute(
                sa.select(tables.zone_presence)
                .where(_P.mower_id == self._mower_id)
                .with_for_update()
            )
        ).first()
        return _presence(row) if row else None

    async def put_presence(self, presence: Presence) -> None:
        values = {f.name: getattr(presence, f.name) for f in dataclasses.fields(presence)}
        insert = pg_insert(tables.zone_presence).values(**values)
        await self._conn.execute(
            insert.on_conflict_do_update(
                index_elements=["mower_id"],
                set_={k: insert.excluded[k] for k in values if k != "mower_id"},
            )
        )

    async def add_time(self, mower_id: UUID, account_id: UUID, time: ZoneTime) -> None:
        exists = (
            await self._conn.execute(
                sa.select(_Z.id).where(_Z.id == time.zone_id, _Z.mower_id == mower_id)
            )
        ).first()
        if exists is None:
            return
        insert = pg_insert(tables.zone_days).values(
            mower_id=mower_id,
            zone_id=time.zone_id,
            day=time.day,
            account_id=account_id,
            seconds=time.seconds,
            mowing_seconds=time.mowing_seconds,
            entries=time.entries,
        )
        await self._conn.execute(
            insert.on_conflict_do_update(
                index_elements=["mower_id", "zone_id", "day"],
                set_={
                    "seconds": tables.zone_days.c.seconds + insert.excluded.seconds,
                    "mowing_seconds": tables.zone_days.c.mowing_seconds
                    + insert.excluded.mowing_seconds,
                    "entries": tables.zone_days.c.entries + insert.excluded.entries,
                },
            )
        )


class SqlZoneStore:
    """Implements :class:`~telemetry.application.zone_ports.ZoneStore`."""

    def __init__(self, engine: AsyncEngine) -> None:
        self.engine = engine

    async def zones_of(self, account_id: UUID, mower_id: UUID) -> list[VirtualZone]:
        async with self.engine.connect() as conn:
            result = await conn.execute(
                sa.select(tables.virtual_zones)
                .where(_Z.account_id == account_id, _Z.mower_id == mower_id)
                .order_by(_Z.created_at, _Z.id)
            )
            return [_zone(r) for r in result]

    async def zones_for(self, mower_id: UUID) -> list[VirtualZone]:
        async with self.engine.connect() as conn:
            result = await conn.execute(
                sa.select(tables.virtual_zones)
                .where(_Z.mower_id == mower_id)
                .order_by(_Z.created_at, _Z.id)
            )
            return [_zone(r) for r in result]

    async def add_zone(self, zone: VirtualZone, limit: int) -> bool:
        async with self.engine.begin() as conn:
            # Serialise inserts per mower so two requests cannot both pass the count.
            await conn.execute(_ZONE_LOCK, {"key": f"zones:{zone.mower_id}"})
            count = (
                await conn.execute(sa.select(sa.func.count()).where(_Z.mower_id == zone.mower_id))
            ).scalar_one()
            if count >= limit:
                return False
            await conn.execute(sa.insert(tables.virtual_zones).values(**_zone_values(zone)))
        return True

    async def put_zone(self, zone: VirtualZone) -> bool:
        values = _zone_values(zone)
        for key in ("id", "account_id", "mower_id", "created_at", "created_by"):
            values.pop(key)
        async with self.engine.begin() as conn:
            result = await conn.execute(
                sa.update(tables.virtual_zones)
                .where(
                    _Z.id == zone.id,
                    _Z.account_id == zone.account_id,
                    _Z.mower_id == zone.mower_id,
                )
                .values(**values)
            )
        return result.rowcount > 0

    async def delete_zone(self, account_id: UUID, mower_id: UUID, zone_id: UUID) -> bool:
        async with self.engine.begin() as conn:
            result = await conn.execute(
                sa.delete(tables.virtual_zones).where(
                    _Z.id == zone_id, _Z.account_id == account_id, _Z.mower_id == mower_id
                )
            )
        return result.rowcount > 0

    @asynccontextmanager
    async def presence_tx(self, mower_id: UUID) -> AsyncIterator[_SqlPresenceTx]:
        async with self.engine.begin() as conn:
            # Also covers the first evaluation, when there is no row to lock yet.
            await conn.execute(_ZONE_LOCK, {"key": f"presence:{mower_id}"})
            yield _SqlPresenceTx(conn, mower_id)

    async def presence_of(self, account_id: UUID, mower_id: UUID) -> Presence | None:
        async with self.engine.connect() as conn:
            row = (
                await conn.execute(
                    sa.select(tables.zone_presence).where(
                        _P.account_id == account_id, _P.mower_id == mower_id
                    )
                )
            ).first()
        return _presence(row) if row else None

    async def days_of(
        self, account_id: UUID, mower_id: UUID, first: date, last: date
    ) -> list[ZoneDay]:
        async with self.engine.connect() as conn:
            result = await conn.execute(
                sa.select(tables.zone_days)
                .where(
                    _D.account_id == account_id,
                    _D.mower_id == mower_id,
                    _D.day >= first,
                    _D.day <= last,
                )
                .order_by(_D.zone_id, _D.day)
            )
            return [ZoneDay(**r._mapping) for r in result]
