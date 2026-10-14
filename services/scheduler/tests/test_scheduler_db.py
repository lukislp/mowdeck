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
from mowdeck_service_kit.db import async_database_url
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from scheduler.adapters.db.migrate import alembic_config
from scheduler.adapters.db.store import SqlStore
from scheduler.adapters.db.tables import metadata
from scheduler.adapters.memory import MemoryStore
from scheduler.application.ports import Store
from scheduler.domain.models import MowerPlace, MowerStatus, Schedule, ScheduleRun

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
        "pattern": {"angle": 10},
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
