"""PostgreSQL: migrations up and down, schema drift, the store contract and ``SKIP LOCKED``.

The PostgreSQL tests run when ``MOWDECK_TEST_DATABASE_URL`` points at a server where the user
may create databases (CI: ``postgresql://postgres:test@127.0.0.1:5433/postgres``). Each test
creates its own database and drops it afterwards. The store contract also runs against the
memory store, so both implementations behave the same. Locally::

    docker run -d --name scheduler-test-pg -p 5433:5432 -e POSTGRES_PASSWORD=test \\
        postgres:17.11-alpine
    MOWDECK_TEST_DATABASE_URL=postgresql://postgres:test@127.0.0.1:5433/postgres \\
        uv run pytest services/scheduler -k db
"""

import asyncio
import json
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, date, datetime, time, timedelta
from typing import Any
from uuid import uuid4

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from mowdeck_contracts.v1 import MowerCapabilities
from mowdeck_service_kit.db import async_database_url
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from scheduler.adapters.db.migrate import alembic_config
from scheduler.adapters.db.store import SqlStore
from scheduler.adapters.db.tables import metadata
from scheduler.adapters.memory import MemoryStore
from scheduler.application.ports import Store
from scheduler.domain.models import MowerPlace, MowerStatus, Schedule, ScheduleRun
from scheduler.domain.rotation import AreaAngle, MowerRotation

ADMIN_URL = os.environ.get("MOWDECK_TEST_DATABASE_URL")
needs_db = pytest.mark.skipif(not ADMIN_URL, reason="needs MOWDECK_TEST_DATABASE_URL")
NOW = datetime(2026, 10, 9, 8, 0, tzinfo=UTC)


@asynccontextmanager
async def temporary_database() -> AsyncIterator[str]:
    """Create a fresh database and yield its URL; drop it afterwards."""
    assert ADMIN_URL
    name = f"scheduler_test_{uuid4().hex[:10]}"
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
async def test_scheduler_db_migrations_up_down_and_no_drift(
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


STORES = [pytest.param("memory"), pytest.param("postgres", marks=needs_db)]


def schedule_row(account_id: Any, name: str, **values: Any) -> Schedule:
    base: dict[str, Any] = {
        "id": uuid4(),
        "account_id": account_id,
        "name": name,
        "target_type": "mower",
        "target_id": uuid4(),
        "kind": "weekly",
        "start_time": time(10, 30),
        "duration_minutes": 60,
        "timezone": "Europe/Berlin",
        "owner": {"subject": str(uuid4()), "kind": "user"},
        "owner_subject": uuid4(),
        "created_at": NOW,
        "updated_at": NOW,
        "days_of_week": [0, 4],
        "rain": {"enabled": True, "probability_threshold": 50},
        "pattern": {"angle_deg": 10.0, "obstacle_detection": "sensitive"},
        "blade_height_mm": 45,
        "speed_mps": 0.6,
        "rotation_angles": [0.0, 90.0],
        "area_ids": ["front"],
        "next_run_at": NOW,
        **values,
    }
    return Schedule(**base)


def run_row(item: Schedule, at: datetime, outcome: str = "started") -> ScheduleRun:
    return ScheduleRun(
        id=uuid4(),
        schedule_id=item.id,
        account_id=item.account_id,
        trigger="schedule",
        scheduled_for=at,
        started_at=at,
        outcome=outcome,
        mower_id=item.target_id,
        issued_by={"subject": str(uuid4()), "kind": "user"},
        window_end_at=at + timedelta(hours=1),
    )


@pytest.mark.parametrize("kind", STORES)
async def test_scheduler_db_store_contract(kind: str, monkeypatch: pytest.MonkeyPatch) -> None:
    account_id, other = uuid4(), uuid4()
    async with store_of(kind, monkeypatch) as store:
        due = schedule_row(account_id, "b")
        later = schedule_row(account_id, "a", next_run_at=NOW + timedelta(hours=1))
        every = schedule_row(
            other, "c", kind="every_n_days", anchor_date=date(2026, 1, 1), interval_days=3
        )
        async with store.transaction() as tx:
            for item in (due, later, every):
                await tx.add_schedule(item)
        async with store.transaction() as tx:
            assert [s.name for s in await tx.list_schedules(account_id)] == ["a", "b"]
            assert await tx.count_schedules(account_id) == 2
            assert await tx.get_schedule(due.id, other) is None
            loaded = await tx.get_schedule(every.id, for_update=True)
            assert loaded == every
            claimed = await tx.claim_due(NOW)
            assert claimed is not None and claimed.id == due.id
            claimed.next_run_at = NOW + timedelta(days=1)
            claimed.skip_until = NOW
            await tx.update_schedule(claimed)
        async with store.transaction() as tx:
            next_due = await tx.claim_due(NOW)
            assert next_due is not None and next_due.id == every.id
            stored = await tx.get_schedule(due.id, account_id)
            assert stored is not None and stored.skip_until == NOW
            runs = [run_row(due, NOW + timedelta(minutes=i)) for i in range(3)]
            runs.append(run_row(due, NOW - timedelta(days=200), "skipped_rain"))
            for run in runs:
                await tx.add_run(run)
        async with store.transaction() as tx:
            page = await tx.list_runs(due.id, account_id, limit=2, before=None)
            assert [r.id for r in page] == [runs[2].id, runs[1].id]
            rest = await tx.list_runs(due.id, account_id, limit=5, before=page[-1].started_at)
            assert [r.id for r in rest] == [runs[0].id, runs[3].id]
            assert await tx.list_runs(due.id, other, limit=5, before=None) == []
            open_runs = await tx.claim_open_runs(10)
            assert {r.id for r in open_runs} == {r.id for r in runs[:3]}
            open_runs[0].followup = "finished"
            open_runs[0].followup_at = NOW
            await tx.update_run(open_runs[0])
        async with store.transaction() as tx:
            assert len(await tx.claim_open_runs(10)) == 2
            assert await tx.purge_runs(NOW - timedelta(days=90)) == 1
            assert await tx.try_lock(42)
            mower_id = uuid4()
            assert await tx.put_status(MowerStatus(mower_id, account_id, "mowing", NOW, 1.0, 2.0))
            assert not await tx.put_status(
                MowerStatus(mower_id, account_id, "docked", NOW - timedelta(minutes=1))
            )
            assert await tx.put_status(
                MowerStatus(mower_id, account_id, "docked", NOW + timedelta(minutes=1))
            )
            status = await tx.get_status(mower_id)
            assert status is not None and status.activity == "docked" and status.latitude is None
            await tx.put_place(MowerPlace(mower_id, account_id, NOW, 48.1, 11.5, ["a"]))
            await tx.put_place(MowerPlace(mower_id, account_id, NOW, 48.2, 11.6, ["a", "b"]))
            place = await tx.get_place(mower_id)
            assert place is not None and place.area_ids == ["a", "b"] and place.latitude == 48.2
            assert await tx.get_status(uuid4()) is None and await tx.get_place(uuid4()) is None
            assert not await tx.delete_schedule(due.id, other)
            assert await tx.delete_schedule(due.id, account_id)
        async with store.transaction() as tx:
            assert await tx.list_runs(due.id, account_id, limit=5, before=None) == []
        with pytest.raises(RuntimeError):
            async with store.transaction() as tx:
                await tx.delete_schedule(later.id, account_id)
                raise RuntimeError("rollback")
        async with store.transaction() as tx:
            assert await tx.get_schedule(later.id) is not None


@pytest.mark.parametrize("kind", STORES)
async def test_scheduler_db_capabilities_keep_the_newest(
    kind: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    mower_id, account_id = uuid4(), uuid4()
    first = MowerCapabilities(native_patterns=frozenset({"stripes"}))
    second = MowerCapabilities(
        native_patterns=frozenset({"stripes", "diagonal"}), blade_height_range_mm=(25, 70)
    )
    async with store_of(kind, monkeypatch) as store, store.transaction() as tx:
        assert await tx.get_capabilities(mower_id) is None
        assert await tx.put_capabilities(mower_id, account_id, NOW, first)
        assert not await tx.put_capabilities(
            mower_id, account_id, NOW - timedelta(minutes=1), second
        )
        assert await tx.get_capabilities(mower_id) == first
        assert await tx.put_capabilities(mower_id, account_id, NOW + timedelta(minutes=1), second)
        assert await tx.get_capabilities(mower_id) == second


@pytest.mark.parametrize("kind", STORES)
async def test_scheduler_db_mow_angles_and_rotation(
    kind: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    mower_id, account_id, schedule_id = uuid4(), uuid4(), uuid4()

    def angle(area: str, value: float, minutes: int, source: str = "schedule") -> AreaAngle:
        return AreaAngle(
            mower_id=mower_id,
            area_id=area,
            account_id=account_id,
            angle_deg=value,
            used_at=NOW + timedelta(minutes=minutes),
            source=source,
            schedule_id=schedule_id if source == "schedule" else None,
        )

    async with store_of(kind, monkeypatch) as store:
        async with store.transaction() as tx:
            assert await tx.get_area_angles(mower_id) == []
            assert await tx.get_rotation(mower_id) is None
            await tx.put_area_angles([])
            await tx.put_area_angles([angle("front", 30, 0), angle("back", 30, 0)])
            await tx.put_area_angles([angle("front", 120, 5, "manual")])
            await tx.put_rotation(MowerRotation(mower_id, account_id, 45.0, NOW))
            await tx.put_rotation(MowerRotation(mower_id, account_id, None, NOW))
        async with store.transaction() as tx:
            assert await tx.get_area_angles(mower_id) == [
                angle("back", 30, 0),
                angle("front", 120, 5, "manual"),
            ]
            assert await tx.get_area_angles(mower_id, ["front", "pond"]) == [
                angle("front", 120, 5, "manual")
            ]
            assert await tx.get_area_angles(uuid4()) == []
            assert await tx.get_rotation(mower_id) == MowerRotation(mower_id, account_id, None, NOW)


@needs_db
async def test_scheduler_db_migration_0004_keeps_schedules_and_runs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Revision 0004 adds step rotation off and no angle to everything stored so far."""
    async with temporary_database() as url:
        await migrate(url, monkeypatch, "0003")
        engine = create_async_engine(async_database_url(url))
        try:
            async with engine.begin() as conn:
                await conn.execute(
                    text(
                        "INSERT INTO schedules (id, account_id, name, target_type, target_id,"
                        " kind, start_time, duration_minutes, timezone, owner, owner_subject,"
                        " created_at, updated_at, rotation_angles) VALUES (:id, :account, 'A',"
                        " 'mower', :target, 'weekly', '10:30', 60, 'Europe/Berlin',"
                        " CAST(:owner AS jsonb), :subject, now(), now(), '[0, 90]'::jsonb)"
                    ),
                    {
                        "id": uuid4(),
                        "account": uuid4(),
                        "target": uuid4(),
                        "owner": json.dumps({"subject": str(uuid4()), "kind": "user"}),
                        "subject": uuid4(),
                    },
                )
            await migrate(url, monkeypatch)
            async with engine.connect() as conn:
                row = (
                    await conn.execute(
                        text("SELECT rotation_angles, rotation_step_deg FROM schedules")
                    )
                ).one()
            assert row.rotation_angles == [0, 90] and row.rotation_step_deg is None
            await migrate(url, monkeypatch, "0003", down=True)
            async with engine.connect() as conn:
                names = await table_names_of(conn)
            assert "mow_angles" not in names and "mower_rotation" not in names
        finally:
            await engine.dispose()


async def table_names_of(conn: Any) -> set[str]:
    rows = await conn.execute(text("SELECT tablename FROM pg_tables WHERE schemaname = 'public'"))
    return {r[0] for r in rows}


@needs_db
async def test_scheduler_db_migration_0003_keeps_existing_schedules(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Revision 0003 rewrites the free patterns of 0002 to ``SchedulePattern`` keys: the legacy
    ``angle`` the web form wrote becomes ``angle_deg`` (as the runner sent it), anything outside
    the contract goes, and every new option stays unset."""
    patterns: dict[str, Any] = {
        "legacy": {"angle": 200},
        "both": {"angle": 10, "angle_deg": 30, "border_laps": 2},
        "foreign": {"spacing_cm": 20, "name": "spiral", "border_laps": 9},
        "options": {"order": "grid_first", "obstacle_laps": 1, "spacing_m": 0.3},
        "none": None,
    }
    expected: dict[str, Any] = {
        "legacy": {"angle_deg": 20.0},
        "both": {"angle_deg": 30.0, "border_laps": 2},
        "foreign": None,
        "options": {"order": "grid_first", "obstacle_laps": 1, "spacing_m": 0.3},
        "none": None,
    }
    async with temporary_database() as url:
        await migrate(url, monkeypatch, "0002")
        engine = create_async_engine(async_database_url(url))
        try:
            async with engine.begin() as conn:
                for name, pattern in patterns.items():
                    await conn.execute(
                        text(
                            "INSERT INTO schedules (id, account_id, name, target_type, target_id,"
                            " kind, start_time, duration_minutes, timezone, owner, owner_subject,"
                            " created_at, updated_at, pattern) VALUES (:id, :account, :name,"
                            " 'mower', :target, 'weekly', '10:30', 60, 'Europe/Berlin',"
                            " CAST(:owner AS jsonb), :subject, now(), now(),"
                            " CAST(:pattern AS jsonb))"
                        ),
                        {
                            "id": uuid4(),
                            "account": uuid4(),
                            "name": name,
                            "target": uuid4(),
                            "owner": json.dumps({"subject": str(uuid4()), "kind": "user"}),
                            "subject": uuid4(),
                            "pattern": None if pattern is None else json.dumps(pattern),
                        },
                    )
            await migrate(url, monkeypatch)
            async with engine.connect() as conn:
                rows = (
                    await conn.execute(
                        text("SELECT name, pattern, blade_height_mm, speed_mps FROM schedules")
                    )
                ).all()
            assert {row.name: row.pattern for row in rows} == expected
            assert all(row.blade_height_mm is None and row.speed_mps is None for row in rows)
            await migrate(url, monkeypatch, "0002", down=True)
            async with engine.connect() as conn:
                count = (await conn.execute(text("SELECT count(*) FROM schedules"))).scalar()
            assert count == len(patterns)
        finally:
            await engine.dispose()


@needs_db
async def test_scheduler_db_due_schedules_are_claimed_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with store_of("postgres", monkeypatch) as store:
        account_id = uuid4()
        first, second = schedule_row(account_id, "a"), schedule_row(account_id, "b")
        async with store.transaction() as tx:
            await tx.add_schedule(first)
            await tx.add_schedule(second)
        async with store.transaction() as one, store.transaction() as two:
            claimed_one = await one.claim_due(NOW)
            claimed_two = await two.claim_due(NOW)
            assert claimed_one is not None and claimed_two is not None
            assert claimed_one.id != claimed_two.id  # SKIP LOCKED: the other replica moves on


async def _fill(store: Store, account_id: Any) -> Schedule:
    """One row in every table of the account (two runs, two angles)."""
    item = schedule_row(account_id, "Morning")
    mower_id = item.target_id
    async with store.transaction() as tx:
        await tx.add_schedule(item)
        await tx.add_run(run_row(item, NOW))
        await tx.add_run(run_row(item, NOW + timedelta(minutes=1), "skipped_rain"))
        await tx.put_status(MowerStatus(mower_id, account_id, "mowing", NOW))
        await tx.put_place(MowerPlace(mower_id, account_id, NOW, 48.1, 11.5, ["front"]))
        await tx.put_capabilities(mower_id, account_id, NOW, MowerCapabilities())
        await tx.put_area_angles(
            [
                AreaAngle(mower_id, area, account_id, 30.0, NOW, "schedule", item.id)
                for area in ("front", "back")
            ]
        )
        await tx.put_rotation(MowerRotation(mower_id, account_id, 45.0, NOW))
    return item


@pytest.mark.parametrize("kind", STORES)
async def test_scheduler_db_purge_account(kind: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """ADR 0076: ``purge_account`` deletes every row of one account in every table and
    nothing of another account; a second purge finds nothing."""
    account_id, other = uuid4(), uuid4()
    async with store_of(kind, monkeypatch) as store:
        gone = await _fill(store, account_id)
        kept = await _fill(store, other)
        async with store.transaction() as tx:
            assert await tx.purge_account(account_id) == 9
            assert await tx.purge_account(account_id) == 0
        async with store.transaction() as tx:
            assert await tx.count_schedules(account_id) == 0
            assert await tx.list_runs(gone.id, account_id, limit=5, before=None) == []
            assert await tx.get_status(gone.target_id) is None
            assert await tx.get_place(gone.target_id) is None
            assert await tx.get_capabilities(gone.target_id) is None
            assert await tx.get_area_angles(gone.target_id) == []
            assert await tx.get_rotation(gone.target_id) is None
            assert await tx.count_schedules(other) == 1
            assert len(await tx.list_runs(kept.id, other, limit=5, before=None)) == 2
            assert await tx.get_status(kept.target_id) is not None
            assert await tx.get_place(kept.target_id) is not None
            assert await tx.get_capabilities(kept.target_id) is not None
            assert len(await tx.get_area_angles(kept.target_id)) == 2
            assert await tx.get_rotation(kept.target_id) is not None
