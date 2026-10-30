"""Zone store contract on the memory store and on PostgreSQL (migration included; ADR 0070)."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, date, datetime, timedelta
from uuid import uuid4

import pytest
from mowdeck_service_kit.db import async_database_url
from sqlalchemy.ext.asyncio import create_async_engine
from test_telemetry_db import migrate, needs_db, temporary_database

from telemetry.adapters.db.store import SqlStore
from telemetry.adapters.db.zone_store import SqlZoneStore
from telemetry.adapters.memory_zones import MemoryZoneStore
from telemetry.application.zone_ports import ZoneStore
from telemetry.domain.zones import Presence, VirtualZone, ZoneTime

NOW = datetime(2026, 10, 30, 12, 0, tzinfo=UTC)
DAY = date(2026, 10, 30)


def a_zone(account_id=None, mower_id=None, **changes) -> VirtualZone:  # type: ignore[no-untyped-def]
    values = {
        "id": uuid4(),
        "account_id": account_id or uuid4(),
        "mower_id": mower_id or uuid4(),
        "name": "Lawn",
        "ring": ((52.0, 13.0), (52.0, 13.001), (52.001, 13.001)),
        "area_m2": 3800.5,
        "created_at": NOW,
        "updated_at": NOW,
        **changes,
    }
    return VirtualZone(**values)


@asynccontextmanager
async def store_of(
    kind: str, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[tuple[ZoneStore, SqlStore | None]]:
    if kind == "memory":
        yield MemoryZoneStore(), None
        return
    async with temporary_database() as url:
        await migrate(url, monkeypatch)
        engine = create_async_engine(async_database_url(url))
        try:
            yield SqlZoneStore(engine), SqlStore(engine)
        finally:
            await engine.dispose()


KINDS = [pytest.param("memory"), pytest.param("sql", marks=needs_db)]


@pytest.mark.parametrize("kind", KINDS)
async def test_zone_store_zones_presence_days_and_cleanup(
    kind: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with store_of(kind, monkeypatch) as (store, sql):
        first = a_zone(start_plan_ref="zone:1")
        account_id, mower_id = first.account_id, first.mower_id
        second = a_zone(account_id, mower_id, name="Back", created_at=NOW + timedelta(seconds=1))
        assert await store.add_zone(first, 2)
        assert await store.add_zone(second, 2)
        assert not await store.add_zone(a_zone(account_id, mower_id), 2)  # the limit
        assert await store.zones_of(account_id, mower_id) == [first, second]
        assert await store.zones_of(uuid4(), mower_id) == []  # account scoped
        assert await store.zones_for(mower_id) == [first, second]

        renamed = a_zone(
            account_id,
            mower_id,
            id=first.id,
            name="Front",
            ring=((52.0, 13.0), (52.0, 13.002), (52.002, 13.002)),
            start_plan_ref=None,
            updated_at=NOW + timedelta(minutes=1),
        )
        assert await store.put_zone(renamed)
        assert (await store.zones_of(account_id, mower_id))[0] == renamed
        assert not await store.put_zone(a_zone(uuid4(), mower_id, id=first.id))  # foreign

        presence = Presence(
            mower_id=mower_id,
            account_id=account_id,
            zone_id=first.id,
            since=NOW,
            observed_at=NOW,
            seen_at=NOW,
            mowing=True,
            lat=52.0005,
            lon=13.0005,
            rtk_fix="fixed",
        )
        async with store.presence_tx(mower_id) as tx:
            assert await tx.presence() is None
            await tx.put_presence(presence)
            await tx.add_time(mower_id, account_id, ZoneTime(first.id, DAY, 30.0, 30.0, 1))
            await tx.add_time(mower_id, account_id, ZoneTime(first.id, DAY, 15.5, 0.0))
            await tx.add_time(mower_id, account_id, ZoneTime(second.id, DAY, 5.0, 0.0, 1))
            await tx.add_time(mower_id, account_id, ZoneTime(uuid4(), DAY, 99.0, 0.0))  # gone
        async with store.presence_tx(mower_id) as tx:
            assert await tx.presence() == presence
            await tx.put_presence(presence)  # an upsert of the same row
        assert await store.presence_of(account_id, mower_id) == presence
        assert await store.presence_of(uuid4(), mower_id) is None

        days = await store.days_of(account_id, mower_id, DAY, DAY)
        by_zone = {d.zone_id: d for d in days}
        assert by_zone[first.id].seconds == 45.5
        assert by_zone[first.id].mowing_seconds == 30.0
        assert by_zone[first.id].entries == 1
        assert by_zone[second.id].seconds == 5.0
        assert len(days) == 2
        assert await store.days_of(account_id, mower_id, DAY + timedelta(days=1), DAY) == []

        assert await store.delete_zone(account_id, mower_id, second.id)
        assert not await store.delete_zone(account_id, mower_id, second.id)
        assert not await store.delete_zone(uuid4(), mower_id, first.id)
        assert [d.zone_id for d in await store.days_of(account_id, mower_id, DAY, DAY)] == [
            first.id
        ]

        # Removing the mower removes everything of it.
        if sql is not None:
            async with sql.transaction() as tx_all:
                await tx_all.drop_mower(mower_id)
        else:
            assert isinstance(store, MemoryZoneStore)
            store.drop_mower(mower_id)
        assert await store.zones_of(account_id, mower_id) == []
        assert await store.presence_of(account_id, mower_id) is None
        assert await store.days_of(account_id, mower_id, DAY, DAY) == []
