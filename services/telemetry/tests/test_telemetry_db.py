"""PostgreSQL: migrations up and down, schema drift, the store contract, retention lock.

The PostgreSQL tests run when ``MOWDECK_TEST_DATABASE_URL`` points at a server where the user
may create databases (CI: ``postgresql://postgres:test@127.0.0.1:5433/postgres``). Each test
creates its own database and drops it afterwards. The store contract tests also run against
the memory store, so both implementations behave the same. Locally::

    docker run -d --name telemetry-test-pg -p 5433:5432 -e POSTGRES_PASSWORD=test \\
        public.ecr.aws/docker/library/postgres:17.11-alpine
    MOWDECK_TEST_DATABASE_URL=postgresql://postgres:test@127.0.0.1:5433/postgres \\
        uv run pytest services/telemetry -k db
"""

import asyncio
import dataclasses
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from mowdeck_contracts.v1 import CommandStatus, MowerActivity
from mowdeck_service_kit.db import async_database_url
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from telemetry_support import API, T0, harness, make_state, seconds

from telemetry.adapters.db.migrate import alembic_config
from telemetry.adapters.db.store import SqlStore
from telemetry.adapters.db.tables import metadata
from telemetry.adapters.memory import MemoryStore
from telemetry.application.context import TelemetryConfig
from telemetry.application.ports import RetentionTarget, Store
from telemetry.application.services import build_services
from telemetry.domain.health import BladeChange, DailyRow
from telemetry.domain.models import CommandEvent, DirectoryEntry, LatestRow, SnapshotRow
from telemetry.domain.rules import downsample, history_row, position_row

ADMIN_URL = os.environ.get("MOWDECK_TEST_DATABASE_URL")
needs_db = pytest.mark.skipif(not ADMIN_URL, reason="needs MOWDECK_TEST_DATABASE_URL")
NOW = datetime(2026, 10, 9, 12, 0, tzinfo=UTC)


@asynccontextmanager
async def temporary_database() -> AsyncIterator[str]:
    """Create a fresh database and yield its URL; drop it afterwards."""
    assert ADMIN_URL
    name = f"telemetry_test_{uuid4().hex[:10]}"
    admin = create_async_engine(async_database_url(ADMIN_URL), isolation_level="AUTOCOMMIT")
    async with admin.connect() as conn:
        await conn.execute(text(f'CREATE DATABASE "{name}"'))
    base, _, _ = ADMIN_URL.rpartition("/")
    try:
        yield f"{base}/{name}"
    finally:
        async with admin.connect() as conn:
            await conn.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        await admin.dispose()


async def migrate(
    url: str, monkeypatch: pytest.MonkeyPatch, revision: str = "head", *, down: bool = False
) -> None:
    monkeypatch.setenv("MOWDECK_DATABASE_URL", url)
    if down:
        await asyncio.to_thread(command.downgrade, alembic_config(), revision)
    else:
        await asyncio.to_thread(command.upgrade, alembic_config(), revision)


async def table_names(engine: AsyncEngine) -> set[str]:
    async with engine.connect() as conn:
        rows = await conn.execute(
            text("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")
        )
        return {row[0] for row in rows}


@needs_db
async def test_telemetry_db_migrations_up_down_and_no_drift(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with temporary_database() as url:
        await migrate(url, monkeypatch)
        engine = create_async_engine(async_database_url(url))
        try:
            assert set(metadata.tables) <= await table_names(engine)

            def diff(sync_conn: Any) -> list[Any]:
                context = MigrationContext.configure(sync_conn)
                return list(compare_metadata(context, metadata))

            async with engine.connect() as conn:
                drift = await conn.run_sync(diff)
            assert drift == []
            await migrate(url, monkeypatch, "base", down=True)
            assert await table_names(engine) == {"alembic_version"}
            await migrate(url, monkeypatch)
            assert set(metadata.tables) <= await table_names(engine)
        finally:
            await engine.dispose()


@asynccontextmanager
async def store_of(kind: str, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[Store]:
    if kind == "memory":
        yield MemoryStore()
        return
    async with temporary_database() as url:
        await migrate(url, monkeypatch)
        engine = create_async_engine(async_database_url(url))
        try:
            yield SqlStore(engine)
        finally:
            await engine.dispose()


KINDS = [
    "memory",
    pytest.param("sql", marks=needs_db),
]


@pytest.mark.parametrize("kind", KINDS)
async def test_telemetry_db_latest_never_goes_back_in_time(
    kind: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    account, mower = uuid4(), uuid4()

    def latest(at: datetime, battery: int) -> LatestRow:
        return LatestRow(
            mower_id=mower,
            account_id=account,
            observed_at=at,
            received_at=at,
            activity="mowing",
            battery_percent=battery,
            state={"battery_percent": battery, "raw": {"a": [1, 2]}},
            history_written_at=at,
            history_fingerprint="f",
            position_fingerprint="p",
        )

    async with store_of(kind, monkeypatch) as store:
        async with store.transaction() as tx:
            assert await tx.latest(mower) is None
            await tx.put_latest(latest(NOW, 80))
        async with store.transaction() as tx:
            await tx.put_latest(latest(NOW - seconds(5), 10))  # older: ignored
            await tx.put_latest(latest(NOW + seconds(5), 70))
        row = await store.latest_of(account, mower)
        assert row is not None
        assert (row.battery_percent, row.state["raw"], row.position_fingerprint) == (
            70,
            {"a": [1, 2]},
            "p",
        )
        assert await store.latest_of(uuid4(), mower) is None  # another account
        assert [r.mower_id for r in await store.latest_many(account, None, 10)] == [mower]
        assert await store.latest_many(account, {uuid4()}, 10) == []
        assert await store.latest_many(account, set(), 10) == []
        assert await store.latest_many(account, {mower}, 10) != []
        assert await store.latest_many(uuid4(), None, 10) == []


@pytest.mark.parametrize("kind", KINDS)
async def test_telemetry_db_link_heartbeat_replaces_the_latest_state_of_the_same_report(
    kind: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    account, mower = uuid4(), uuid4()

    def latest(heartbeat: datetime, *, connected: bool = True) -> LatestRow:
        return LatestRow(
            mower_id=mower,
            account_id=account,
            observed_at=NOW,  # the mower's report stays, only the link heartbeat moves
            received_at=heartbeat,
            activity="docked",
            battery_percent=90,
            state={"heartbeat_at": heartbeat.isoformat(), "connected": connected},
            history_written_at=NOW,
            history_fingerprint="f",
            position_fingerprint="p",
        )

    async with store_of(kind, monkeypatch) as store:
        async with store.transaction() as tx:
            await tx.put_latest(latest(NOW))
        async with store.transaction() as tx:
            await tx.put_latest(latest(NOW + seconds(10)))
        async with store.transaction() as tx:
            await tx.put_latest(latest(NOW + seconds(10), connected=False))
        row = await store.latest_of(account, mower)
    assert row is not None
    assert row.state == {"heartbeat_at": (NOW + seconds(10)).isoformat(), "connected": False}


@pytest.mark.parametrize("kind", KINDS)
async def test_telemetry_db_history_positions_commands_and_isolation(
    kind: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    account, other, mower = uuid4(), uuid4(), uuid4()
    base = datetime(2026, 10, 9, 12, 0, tzinfo=UTC)
    states = [
        make_state(
            mower,
            account,
            at=base + seconds(i),
            battery=90 - i,
            position=(48.0 + i / 1000, 11.0),
            charging=i == 0,
        )
        for i in range(8)
    ]
    command_id = uuid4()
    async with store_of(kind, monkeypatch) as store:
        async with store.transaction() as tx:
            for state in states:
                await tx.add_history(history_row(state))
                position = position_row(state)
                assert position is not None
                await tx.add_position(position)
                await tx.add_history(history_row(state))  # repeated: ignored
                await tx.add_position(position)
            for status, offset in (("accepted", 1), ("executed", 3)):
                event = CommandEvent(account, mower, command_id, status, base + seconds(offset))
                await tx.add_command_event(event)
                await tx.add_command_event(event)
        end = base + seconds(60)
        raw = await store.history(account, mower, base, end, None, 100)
        binned = await store.history(account, mower, base, end, seconds(5), 100)
        limited = await store.history(account, mower, base, end, None, 3)
        narrow = await store.history(account, mower, base + seconds(2), base + seconds(4), None, 9)
        foreign = await store.history(other, mower, base, end, None, 100)
        positions = await store.positions(account, mower, base, end, 3)
        all_positions = await store.positions(account, mower, base, end, 100)
        foreign_positions = await store.positions(other, mower, base, end, 100)
        commands = await store.commands(account, mower, 10)
        foreign_commands = await store.commands(other, mower, 10)
    assert [p.battery_percent for p in raw] == [90, 89, 88, 87, 86, 85, 84, 83]
    assert len(limited) == 3 and len(narrow) == 2 and foreign == []
    expected = downsample(
        [history_row(s) for s in states],
        [position_row(s) for s in states if position_row(s)],  # type: ignore[misc]
        seconds(5),
    )
    assert [(p.at, p.samples, p.battery_percent, p.battery_min, p.battery_max) for p in binned] == [
        (p.at, p.samples, p.battery_percent, p.battery_min, p.battery_max) for p in expected
    ]
    assert [(p.activity, p.charging, p.rtk_fix, p.error_code) for p in binned] == [
        (p.activity, p.charging, p.rtk_fix, p.error_code) for p in expected
    ]
    assert [(p.lat, p.lon) for p in binned] == [(p.lat, p.lon) for p in expected]
    assert sum(p.samples for p in binned) == 8
    assert [p.lat for p in positions] == [48.005, 48.006, 48.007]  # newest 3, oldest first
    assert len(all_positions) == 8 and foreign_positions == []
    assert [c.status for c in commands] == ["executed", "accepted"] and foreign_commands == []


@pytest.mark.parametrize("kind", KINDS)
async def test_telemetry_db_position_chunks_stream_newest_first_with_signals(
    kind: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    account, other, mower = uuid4(), uuid4(), uuid4()
    base = datetime(2026, 10, 9, 12, 0, tzinfo=UTC)
    states = [
        make_state(
            mower,
            account,
            at=base + seconds(i),
            activity=MowerActivity.MOWING if i % 2 else MowerActivity.DOCKED,
            position=(48.0 + i / 1000, 11.0),
            raw={"wifi_rssi": -40 - i, "mnet_rssi": 0},
        )
        for i in range(11)
    ]
    async with store_of(kind, monkeypatch) as store:
        async with store.transaction() as tx:
            for state in states:
                position = position_row(state)
                assert position is not None
                await tx.add_position(position)
        end = base + seconds(60)
        chunks = [chunk async for chunk in store.position_chunks(account, mower, base, end, 9, 4)]
        foreign = [c async for c in store.position_chunks(other, mower, base, end, 9, 4)]
    assert [len(c) for c in chunks] == [4, 4, 1] and foreign == []
    rows = [r for c in chunks for r in c]
    assert [r.observed_at for r in rows] == [base + seconds(i) for i in range(10, 1, -1)]
    assert [(r.wifi_rssi, r.mnet_rssi, r.blades_on) for r in rows[:2]] == [
        (-50, None, False),
        (-49, None, True),
    ]


@pytest.mark.parametrize("kind", KINDS)
async def test_telemetry_db_directory_and_purge(kind: str, monkeypatch: pytest.MonkeyPatch) -> None:
    account, other_account, mower, kept = uuid4(), uuid4(), uuid4(), uuid4()
    async with store_of(kind, monkeypatch) as store:
        async with store.transaction() as tx:
            assert await tx.directory_account(mower) is None
            await tx.put_directory(DirectoryEntry(mower, account, NOW), replace=False)
            await tx.put_directory(DirectoryEntry(mower, other_account, NOW), replace=False)
            assert await tx.directory_account(mower) == account  # seed keeps what is there
            await tx.put_directory(DirectoryEntry(mower, other_account, NOW), replace=True)
            assert await tx.directory_account(mower) == other_account
            await tx.put_directory(DirectoryEntry(kept, account, NOW), replace=True)
            for who in (mower, kept):
                state = make_state(who, account, position=(1.0, 2.0))
                await tx.add_history(history_row(state))
                row = position_row(state)
                assert row is not None
                await tx.add_position(row)
                await tx.add_command_event(CommandEvent(account, who, uuid4(), "executed", NOW))
                await tx.put_latest(
                    LatestRow(who, account, NOW, NOW, "idle", 1, {}, None, None, None)
                )
            await tx.drop_mower(mower)
            assert await tx.directory_account(mower) is None
            assert await tx.directory_account(kept) == account
        assert await store.latest_of(account, mower) is None
        assert await store.latest_of(account, kept) is not None
        assert (
            await store.history(account, mower, NOW - seconds(1), NOW + seconds(1), None, 9) == []
        )
        assert await store.commands(account, mower, 9) == []
        assert len(await store.commands(account, kept, 9)) == 1


@pytest.mark.parametrize("kind", KINDS)
async def test_telemetry_db_snapshots_keep_the_newest_and_are_purged(
    kind: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    account, mower = uuid4(), uuid4()

    def row(at: datetime, revision: str) -> SnapshotRow:
        return SnapshotRow(mower, "map", account, at, NOW, revision, {"revision": revision})

    async with store_of(kind, monkeypatch) as store:
        async with store.transaction() as tx:
            await tx.put_snapshot(row(NOW, "b"))
            await tx.put_snapshot(row(NOW - seconds(5), "a"))  # older: ignored
        found = await store.snapshot_of(account, mower, "map")
        foreign = await store.snapshot_of(uuid4(), mower, "map")
        other_kind = await store.snapshot_of(account, mower, "capabilities")
        async with store.transaction() as tx:
            await tx.drop_mower(mower)
        gone = await store.snapshot_of(account, mower, "map")
    assert found is not None and found.revision == "b" and found.body == {"revision": "b"}
    assert foreign is None and other_kind is None and gone is None


@pytest.mark.parametrize("kind", KINDS)
async def test_telemetry_db_rolls_back_a_failed_transaction(
    kind: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    account, mower = uuid4(), uuid4()
    async with store_of(kind, monkeypatch) as store:
        with pytest.raises(RuntimeError, match="boom"):
            async with store.transaction() as tx:
                await tx.put_directory(DirectoryEntry(mower, account, NOW), replace=True)
                raise RuntimeError("boom")
        async with store.transaction() as tx:
            assert await tx.directory_account(mower) is None


@pytest.mark.parametrize("kind", KINDS)
async def test_telemetry_db_retention_deletes_in_batches_and_one_replica_at_a_time(
    kind: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    account, mower = uuid4(), uuid4()
    async with store_of(kind, monkeypatch) as store:
        async with store.transaction() as tx:
            for i in range(25):
                state = make_state(mower, account, at=NOW - timedelta(days=40) + seconds(i))
                await tx.add_history(history_row(state))
            fresh = make_state(mower, account, at=NOW, position=(1.0, 2.0))
            await tx.add_history(history_row(fresh))
            row = position_row(fresh)
            assert row is not None
            await tx.add_position(row)
            await tx.add_command_event(
                CommandEvent(account, mower, uuid4(), "executed", NOW - timedelta(days=40))
            )
            await tx.add_command_event(CommandEvent(account, mower, uuid4(), "executed", NOW))
        cutoff = NOW - timedelta(days=30)
        async with store.retention() as scope:
            assert scope is not None
            deleted_history = await scope.delete_before(RetentionTarget.HISTORY, cutoff, 10)
            deleted_positions = await scope.delete_before(RetentionTarget.POSITIONS, cutoff, 10)
            deleted_commands = await scope.delete_before(RetentionTarget.COMMANDS, cutoff, 10)
            again = await scope.delete_before(RetentionTarget.HISTORY, cutoff, 10)
        remaining = await store.history(
            account, mower, NOW - timedelta(days=60), NOW + seconds(1), None, 99
        )
        commands = await store.commands(account, mower, 9)
    assert (deleted_history, deleted_positions, deleted_commands, again) == (25, 0, 1, 0)
    assert len(remaining) == 1 and len(commands) == 1


@needs_db
async def test_telemetry_db_the_advisory_lock_admits_one_replica(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with temporary_database() as url:
        await migrate(url, monkeypatch)
        first_engine = create_async_engine(async_database_url(url))
        second_engine = create_async_engine(async_database_url(url))
        try:
            first, second = SqlStore(first_engine), SqlStore(second_engine)
            async with first.retention() as holder:
                assert holder is not None
                async with second.retention() as blocked:
                    assert blocked is None
            async with second.retention() as released:  # the lock is free again
                assert released is not None
        finally:
            await first_engine.dispose()
            await second_engine.dispose()


@needs_db
async def test_telemetry_db_full_flow_over_http_with_postgres(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with temporary_database() as url:
        await migrate(url, monkeypatch)
        engine = create_async_engine(async_database_url(url))
        try:
            async with harness(store=SqlStore(engine)) as h:
                owner = await h.owner()
                mower_id = await h.add_mower(owner)
                for i in range(12):
                    await h.publish_state(
                        make_state(
                            mower_id,
                            owner.account_id,
                            at=T0 + seconds(i),
                            battery=90 - i,
                            position=(48.0 + i / 1000, 11.0),
                        )
                    )
                await h.publish_result(mower_id, CommandStatus.EXECUTED)
                params = {
                    "from": T0.strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "to": (T0 + seconds(60)).strftime("%Y-%m-%dT%H:%M:%SZ"),
                }
                base = f"{API}/mowers/{mower_id}"
                state = await h.client.get(f"{base}/state", headers=owner.headers)
                binned = await h.client.get(
                    f"{base}/history", headers=owner.headers, params={**params, "step": "5s"}
                )
                track = await h.client.get(
                    f"{base}/positions", headers=owner.headers, params=params
                )
                timeline = await h.client.get(f"{base}/commands", headers=owner.headers)
                listing = await h.client.get(
                    f"{API}/accounts/me/mowers/states", headers=owner.headers
                )
                intruder = await h.owner()
                foreign = await h.client.get(f"{base}/state", headers=intruder.headers)
        finally:
            await engine.dispose()
    assert state.status_code == 200 and state.json()["state"]["battery_percent"] == 79
    assert [p["samples"] for p in binned.json()["points"]] == [5, 5, 2]
    assert len(track.json()["positions"]) == 12
    assert [i["status"] for i in timeline.json()["items"]] == ["executed"]
    assert len(listing.json()["items"]) == 1 and foreign.status_code == 404


@pytest.mark.parametrize("kind", KINDS)
async def test_telemetry_db_health_totals_days_and_blade_changes(
    kind: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    account, other, mower, kept = uuid4(), uuid4(), uuid4(), uuid4()
    async with store_of(kind, monkeypatch) as store:
        services = build_services(store, TelemetryConfig(), lambda: NOW)
        for who in (mower, kept):
            for i in range(2):
                await services.ingest.record_state(
                    make_state(
                        who,
                        account,
                        at=NOW + seconds(i * 10),
                        activity=MowerActivity.MOWING,
                        position=(48.0 + i / 10_000, 11.0),
                    )
                )
        await services.ingest.record_state(
            make_state(
                mower,
                account,
                at=NOW + timedelta(days=1),
                activity=MowerActivity.ERROR,
                error_code="E1",
            )
        )
        reset = await services.health.reset_blade(account, mower, uuid4(), "fresh")
        await services.health.reset_blade(account, mower, None, None)
        totals = await store.health_of(account, mower)
        foreign = await store.health_of(other, mower)
        first, last = NOW.date(), (NOW + timedelta(days=1)).date()
        days = await store.daily_of(account, mower, first, last)
        only_first = await store.daily_of(account, mower, first, first)
        foreign_days = await store.daily_of(other, mower, first, last)
        changes = await store.blade_changes(account, mower, 10)
        limited = await store.blade_changes(account, mower, 1)
        foreign_changes = await store.blade_changes(other, mower, 10)
        many = await store.daily_many(account, None, first, last, 10)
        chosen = await store.daily_many(account, {kept}, first, last, 10)
        capped = await store.daily_many(account, None, first, last, 2)
        none_chosen = await store.daily_many(account, set(), first, last, 10)
        foreign_many = await store.daily_many(other, None, first, last, 10)
        async with store.transaction() as tx:
            await tx.add_blade_change(changes[0])  # repeated id: ignored
            await tx.drop_mower(mower)
        gone = await store.health_of(account, mower)
        gone_days = await store.daily_of(account, mower, first, last)
        gone_changes = await store.blade_changes(account, mower, 10)
        still = await store.health_of(account, kept)
    assert totals is not None and foreign is None
    assert totals.mowing_seconds == 10 and totals.blade_seconds == 10
    assert 11.0 < totals.distance_m < 11.2 and totals.errors == {"E1": 1}
    assert totals.since == NOW and totals.last_error_code == "E1"
    assert totals.blade_seconds_since_change == 0 and totals.blade_changed_at == NOW
    assert reset.blade_changed_at == NOW
    assert [d.day for d in days] == [first, last] and [d.day for d in only_first] == [first]
    assert isinstance(days[0], DailyRow) and days[1].errors == {"E1": 1}
    assert days[0].mowing_seconds == 10 and foreign_days == []
    assert len(changes) == 2 and len(limited) == 1 and foreign_changes == []
    assert sorted((str(d.mower_id), d.day) for d in many) == [
        (str(d.mower_id), d.day) for d in many
    ]
    assert len(many) == 3 and {d.mower_id for d in chosen} == {kept} and len(capped) == 2
    assert none_chosen == [] and foreign_many == []
    assert days[0].jobs == 1 and days[0].online_seconds == 10
    assert {c.note for c in changes} == {"fresh", None}
    assert all(isinstance(c, BladeChange) for c in changes)
    assert gone is None and gone_days == [] and gone_changes == []
    assert still is not None and still.distance_m > 0


@pytest.mark.parametrize("kind", KINDS)
async def test_telemetry_db_health_rolls_back_with_the_transaction(
    kind: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    account, mower = uuid4(), uuid4()
    async with store_of(kind, monkeypatch) as store:
        services = build_services(store, TelemetryConfig(), lambda: NOW)
        await services.ingest.record_state(make_state(mower, account, at=NOW))
        before = await store.health_of(account, mower)
        assert before is not None
        with pytest.raises(RuntimeError, match="boom"):
            async with store.transaction() as tx:
                row = await tx.health(mower)
                assert row is not None
                await tx.put_health(dataclasses.replace(row, battery_cycles=7))
                day = await tx.daily(mower, NOW.date())
                assert day is not None
                await tx.put_daily(dataclasses.replace(day, charge_sessions=9))
                raise RuntimeError("boom")
        after = await store.health_of(account, mower)
        days = await store.daily_of(account, mower, NOW.date(), NOW.date())
    assert after == before
    assert [d.charge_sessions for d in days] == [0]


@pytest.mark.parametrize("kind", KINDS)
async def test_telemetry_db_health_stores_the_device_blade_counter(
    kind: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    account, mower = uuid4(), uuid4()
    async with store_of(kind, monkeypatch) as store:
        services = build_services(store, TelemetryConfig(), lambda: NOW)
        await services.ingest.record_state(make_state(mower, account, at=NOW))
        without = await store.health_of(account, mower)
        await services.ingest.record_state(
            make_state(
                mower,
                account,
                at=NOW + seconds(10),
                raw={"blade_used_s": 7200, "blade_used_warn_s": 360000},
            )
        )
        stored = await store.health_of(account, mower)
    assert without is not None and without.device_blade_seconds is None
    assert stored is not None
    assert (stored.device_blade_seconds, stored.device_blade_warn_seconds) == (7200, 360000)


@pytest.mark.parametrize("kind", KINDS)
async def test_telemetry_db_health_stores_the_device_battery_cycle_counter(
    kind: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    account, mower = uuid4(), uuid4()
    async with store_of(kind, monkeypatch) as store:
        services = build_services(store, TelemetryConfig(), lambda: NOW)
        await services.ingest.record_state(make_state(mower, account, at=NOW))
        without = await store.health_of(account, mower)
        await services.ingest.record_state(
            make_state(mower, account, at=NOW + seconds(10), raw={"bat_cycles": 152})
        )
        stored = await store.health_of(account, mower)
    assert without is not None and without.device_battery_cycles is None
    assert stored is not None and stored.device_battery_cycles == 152


@pytest.mark.parametrize("kind", KINDS)
async def test_telemetry_db_health_stores_the_blade_threshold(
    kind: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    account, mower = uuid4(), uuid4()
    async with store_of(kind, monkeypatch) as store:
        services = build_services(store, TelemetryConfig(), lambda: NOW)
        await services.health.set_blade_threshold(account, mower, 12.5)
        await services.ingest.record_state(make_state(mower, account, at=NOW))
        stored = await store.health_of(account, mower)
        await services.health.set_blade_threshold(account, mower, None)
        cleared = await store.health_of(account, mower)
    assert stored is not None and stored.blade_threshold_seconds == 12.5 * 3600
    assert stored.since == NOW
    assert cleared is not None and cleared.blade_threshold_seconds is None


@pytest.mark.parametrize("kind", KINDS)
async def test_telemetry_db_health_stores_the_device_lifetime_totals(
    kind: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    account, mower = uuid4(), uuid4()
    async with store_of(kind, monkeypatch) as store:
        services = build_services(store, TelemetryConfig(), lambda: NOW)
        await services.ingest.record_state(make_state(mower, account, at=NOW))
        without = await store.health_of(account, mower)
        await services.ingest.record_state(
            make_state(
                mower,
                account,
                at=NOW + seconds(10),
                raw={"work_time_s": 1_234_567, "mileage_m": 456_789},
            )
        )
        stored = await store.health_of(account, mower)
    assert without is not None
    assert (without.device_mowing_seconds, without.device_distance_m) == (None, None)
    assert stored is not None
    assert (stored.device_mowing_seconds, stored.device_distance_m) == (1_234_567, 456_789)
