"""Demo sandboxes (ADR 0076): the purge of one expired visitor account.

The API tests run the real app on the memory stores; the store test runs the same purge on
the memory stores and, with ``MOWDECK_TEST_DATABASE_URL``, on PostgreSQL (see
``test_telemetry_db``).
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
from mowdeck_control.history import simulated_history
from mowdeck_service_kit.db import async_database_url
from mowdeck_service_kit.demo import DEMO_SEED_HEADER
from sqlalchemy.ext.asyncio import create_async_engine
from telemetry_support import API, harness
from test_telemetry_db import migrate, needs_db, temporary_database

from telemetry.adapters.db.store import SqlStore
from telemetry.adapters.db.tracking_store import SqlTrackingStore
from telemetry.adapters.db.zone_store import SqlZoneStore
from telemetry.adapters.memory import MemoryStore
from telemetry.adapters.memory_tracking import MemoryTrackingStore
from telemetry.adapters.memory_zones import MemoryZoneStore
from telemetry.application.context import TelemetryConfig
from telemetry.application.demo import DemoPurge
from telemetry.application.ports import Store
from telemetry.application.services import build_services
from telemetry.application.tracking_ports import TrackingStore
from telemetry.application.zone_ports import ZoneStore
from telemetry.domain.health import BladeChange
from telemetry.domain.models import CommandEvent, DirectoryEntry, SnapshotRow
from telemetry.domain.thinning import ThinPolicy
from telemetry.domain.tracking import TrackingLink, TrackingPhoto, TrackingSession
from telemetry.domain.zones import Presence, VirtualZone, ZoneTime

SEED = "demo-seed-secret"
DEMO: dict[str, Any] = {"demo_mode": True, "demo_seed_token": SEED}
NOW = datetime(2026, 10, 30, 12, 0, tzinfo=UTC)
TODAY = NOW.date()


def purge_path(account_id: UUID) -> str:
    return f"{API}/demo/accounts/{account_id}"


async def add_rows(store: Store, account_id: UUID, mower_id: UUID) -> None:
    """A command transition, a map snapshot, a blade change and a thinning watermark."""
    async with store.transaction() as tx:
        await tx.add_command_event(CommandEvent(account_id, mower_id, uuid4(), "accepted", NOW))
        await tx.put_snapshot(SnapshotRow(mower_id, "map", account_id, NOW, NOW, "r1", {"a": 1}))
        await tx.add_blade_change(BladeChange(uuid4(), mower_id, account_id, NOW))
    async with store.retention() as scope:
        assert scope is not None
        await scope.thin_slice(mower_id, NOW - timedelta(hours=1), NOW, ThinPolicy())


async def add_extras(
    zones: ZoneStore, tracking: TrackingStore, account_id: UUID, mower_id: UUID
) -> None:
    """A zone with presence and day totals, and a tracking session with a link and a photo."""
    zone = VirtualZone(
        id=uuid4(),
        account_id=account_id,
        mower_id=mower_id,
        name="Lawn",
        ring=((52.0, 13.0), (52.0, 13.001), (52.001, 13.001)),
        area_m2=100.0,
        created_at=NOW,
        updated_at=NOW,
    )
    assert await zones.add_zone(zone, 10)
    async with zones.presence_tx(mower_id) as tx:
        await tx.put_presence(
            Presence(
                mower_id=mower_id,
                account_id=account_id,
                zone_id=zone.id,
                since=NOW,
                observed_at=NOW,
                seen_at=NOW,
                mowing=True,
                lat=52.0005,
                lon=13.0005,
                rtk_fix="fixed",
            )
        )
        await tx.add_time(mower_id, account_id, ZoneTime(zone.id, TODAY, 30.0, 30.0, 1))
    session = TrackingSession(
        id=uuid4(),
        account_id=account_id,
        mower_id=mower_id,
        trigger="manual",
        started_at=NOW,
        ends_at=NOW + timedelta(hours=24),
        interval_seconds=10.0,
        mower_name="Front",
    )
    assert await tracking.add_session(session)
    await tracking.add_link(
        TrackingLink(
            id=uuid4(),
            session_id=session.id,
            account_id=account_id,
            mower_id=mower_id,
            token_hash=uuid4().hex * 2,
            created_at=NOW,
            expires_at=NOW + timedelta(hours=48),
        )
    )
    await tracking.add_photo(
        TrackingPhoto(
            id=uuid4(),
            session_id=session.id,
            account_id=account_id,
            taken_at=NOW,
            media_type="image/jpeg",
            data=b"\xff\xd8jpeg",
        )
    )


async def everything(
    store: Store, zones: ZoneStore, tracking: TrackingStore, account_id: UUID, mower_id: UUID
) -> dict[str, Any]:
    """What the stores hand out about one mower of one account."""
    # The demo route simulates days up to the real clock: look at all of time.
    start, end = datetime(2000, 1, 1, tzinfo=UTC), datetime(2100, 1, 1, tzinfo=UTC)
    sessions = await tracking.sessions_of(account_id, mower_id, 100)
    async with store.retention() as scope:
        assert scope is not None
        watermark = await scope.thin_start(mower_id)
    return {
        "directory": await store.directory_of(mower_id),
        "latest": await store.latest_of(account_id, mower_id),
        "history": await store.history(account_id, mower_id, start, end, None, 100_000),
        "positions": await store.positions(account_id, mower_id, start, end, 100_000),
        "commands": await store.commands(account_id, mower_id, 100),
        "snapshot": await store.snapshot_of(account_id, mower_id, "map"),
        "health": await store.health_of(account_id, mower_id),
        "daily": await store.daily_of(account_id, mower_id, start.date(), end.date()),
        "blades": await store.blade_changes(account_id, mower_id, 100),
        "watermark": watermark,
        "zones": await zones.zones_of(account_id, mower_id),
        "presence": await zones.presence_of(account_id, mower_id),
        "zone_days": await zones.days_of(account_id, mower_id, start.date(), end.date()),
        "sessions": sessions,
        "links": [link for s in sessions for link in await tracking.links_of(s.id)],
        "photos": [p for s in sessions for p in await tracking.photos_of(s.id)],
    }


def empty(found: dict[str, Any]) -> bool:
    return all(not value for value in found.values())


# --- the endpoint --------------------------------------------------------------------------


async def test_purge_is_a_demo_only_seed_endpoint() -> None:
    account_id = uuid4()
    async with harness(demo_seed_token=SEED) as h:
        outside = await h.client.delete(purge_path(account_id), headers={DEMO_SEED_HEADER: SEED})
    async with harness(**DEMO) as h:
        without = await h.client.delete(purge_path(account_id))
        wrong = await h.client.delete(purge_path(account_id), headers={DEMO_SEED_HEADER: "nope"})
        bad_id = await h.client.delete(
            f"{API}/demo/accounts/not-a-uuid", headers={DEMO_SEED_HEADER: SEED}
        )
    assert outside.status_code == 404
    assert without.status_code == 403
    assert wrong.status_code == 403
    assert bad_id.status_code == 422


async def test_purge_removes_one_account_and_keeps_the_other() -> None:
    zones, tracking = MemoryZoneStore(), MemoryTrackingStore()
    async with harness(**DEMO, zone_store=zones, tracking_store=tracking) as h:
        mowers: dict[UUID, UUID] = {}
        for _ in range(2):
            owner = await h.owner()
            mower_id = await h.add_mower(owner)
            mowers[owner.account_id] = mower_id
            body = {
                "mowers": [{"mower_id": str(mower_id), "account_id": str(owner.account_id)}],
                "days": 1,
            }
            seeded = await h.client.post(
                f"{API}/demo/history", json=body, headers={DEMO_SEED_HEADER: SEED}
            )
            assert seeded.status_code == 200, seeded.text
            await add_rows(h.store, owner.account_id, mower_id)
            await add_extras(zones, tracking, owner.account_id, mower_id)
            assert h.store.data.directory[mower_id].account_id == owner.account_id
        (gone, gone_mower), (kept, kept_mower) = mowers.items()
        assert gone_mower in h.store.data.thinned_until
        before = await everything(h.store, zones, tracking, kept, kept_mower)
        assert all(before.values())

        first = await h.client.delete(purge_path(gone), headers={DEMO_SEED_HEADER: SEED})
        second = await h.client.delete(purge_path(gone), headers={DEMO_SEED_HEADER: SEED})
        nobody = await h.client.delete(purge_path(uuid4()), headers={DEMO_SEED_HEADER: SEED})

        assert first.status_code == 200, first.text
        assert first.json()["deleted"] > 100
        assert second.status_code == 200
        assert second.json() == {"deleted": 0}
        assert nobody.json() == {"deleted": 0}
        assert empty(await everything(h.store, zones, tracking, gone, gone_mower))
        assert gone_mower not in h.store.data.thinned_until
        data = h.store.data
        rows: list[Any] = [
            *data.latest.values(),
            *data.history.values(),
            *data.positions.values(),
            *data.commands.values(),
            *data.directory.values(),
            *data.snapshots.values(),
            *data.health.values(),
            *data.daily.values(),
            *data.blade_changes.values(),
            *zones.zones.values(),
            *zones.presences.values(),
            *zones.days.values(),
            *tracking.sessions.values(),
            *tracking.links.values(),
            *tracking.photos,
        ]
        assert all(row.account_id != gone for row in rows)
        assert await everything(h.store, zones, tracking, kept, kept_mower) == before


# --- the stores (memory and PostgreSQL) ----------------------------------------------------


@asynccontextmanager
async def stores_of(
    kind: str, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[tuple[Store, ZoneStore, TrackingStore]]:
    if kind == "memory":
        yield MemoryStore(), MemoryZoneStore(), MemoryTrackingStore()
        return
    async with temporary_database() as url:
        await migrate(url, monkeypatch)
        engine = create_async_engine(async_database_url(url))
        try:
            yield SqlStore(engine), SqlZoneStore(engine), SqlTrackingStore(engine)
        finally:
            await engine.dispose()


async def fill(store: Store, zones: ZoneStore, tracking: TrackingStore, account_id: UUID) -> UUID:
    """A mower with one simulated day and a row in every table."""
    mower_id = uuid4()
    services = build_services(store, TelemetryConfig(), lambda: NOW)
    async with store.transaction() as tx:
        await tx.put_directory(DirectoryEntry(mower_id, account_id, NOW, "Front"), replace=True)
    for state in await simulated_history(
        mower_id=mower_id, account_id=account_id, options={}, end=NOW, days=1
    ):
        await services.ingest.record_state(state)
    await add_rows(store, account_id, mower_id)
    await add_extras(zones, tracking, account_id, mower_id)
    return mower_id


KINDS = [pytest.param("memory"), pytest.param("sql", marks=needs_db)]


@pytest.mark.parametrize("kind", KINDS)
async def test_store_purge_deletes_every_table_of_one_account(
    kind: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with stores_of(kind, monkeypatch) as (store, zones, tracking):
        purge = DemoPurge(store, zones, tracking)
        gone, kept = uuid4(), uuid4()
        gone_mower = await fill(store, zones, tracking, gone)
        kept_mower = await fill(store, zones, tracking, kept)
        before = await everything(store, zones, tracking, kept, kept_mower)
        assert all(before.values())
        assert all((await everything(store, zones, tracking, gone, gone_mower)).values())

        deleted = await purge.purge_account(gone)

        assert deleted > 100
        assert empty(await everything(store, zones, tracking, gone, gone_mower))
        assert await everything(store, zones, tracking, kept, kept_mower) == before
        assert await purge.purge_account(gone) == 0
        assert await DemoPurge(store).purge_account(uuid4()) == 0
