"""PostgreSQL: migrations up and down, schema drift, the store contract, a full flow.

The PostgreSQL tests run when ``MOWDECK_TEST_DATABASE_URL`` points at a server where the user
may create databases (CI: ``postgresql://postgres:test@127.0.0.1:5433/postgres``). Each test
creates its own database and drops it afterwards. The store contract tests also run against
the memory store, so both implementations behave the same. Locally::

    docker run -d --name fleet-test-pg -p 5433:5432 -e POSTGRES_PASSWORD=test \\
        public.ecr.aws/docker/library/postgres:17.11-alpine
    MOWDECK_TEST_DATABASE_URL=postgresql://postgres:test@127.0.0.1:5433/postgres \\
        uv run pytest services/fleet -k db
"""

import asyncio
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
from fleet_support import API, fake_gateway, harness
from mowdeck_service_kit.db import async_database_url
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from fleet.adapters.db.migrate import alembic_config
from fleet.adapters.db.store import SqlStore
from fleet.adapters.db.tables import metadata
from fleet.adapters.memory import MemoryStore
from fleet.application.drive import drive_lock_key
from fleet.application.ports import Gt, In, Lt, Store
from fleet.domain.errors import ConflictError
from fleet.domain.models import (
    AuditEntry,
    CommandRecord,
    DriveSession,
    Group,
    LinkedAccount,
    Mower,
    MowerAreaName,
    MowerGroup,
    Site,
)

ADMIN_URL = os.environ.get("MOWDECK_TEST_DATABASE_URL")
needs_db = pytest.mark.skipif(not ADMIN_URL, reason="needs MOWDECK_TEST_DATABASE_URL")
NOW = datetime(2026, 10, 9, 12, 0, tzinfo=UTC)


@asynccontextmanager
async def temporary_database() -> AsyncIterator[str]:
    """Create a fresh database and yield its URL; drop it afterwards."""
    assert ADMIN_URL
    name = f"fleet_test_{uuid4().hex[:10]}"
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
async def test_fleet_db_migrations_up_down_and_no_drift(monkeypatch: pytest.MonkeyPatch) -> None:
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
            async with engine.begin() as conn:
                await conn.execute(
                    text(
                        "INSERT INTO audit_log (at, action, account_id, target_type, target_id, "
                        "details) VALUES (now(), 'x.y', gen_random_uuid(), 'mower', '1', '{}')"
                    )
                )
            for statement in ("UPDATE audit_log SET action = 'z.z'", "DELETE FROM audit_log"):
                with pytest.raises(DBAPIError, match="append-only"):
                    async with engine.begin() as conn:
                        await conn.execute(text(statement))
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


def site_row(account_id: Any, name: str, *, default: bool = False) -> Site:
    return Site(
        id=uuid4(),
        account_id=account_id,
        name=name,
        timezone="Europe/Berlin",
        is_default=default,
        created_at=NOW,
        updated_at=NOW,
    )


def record(account_id: Any, mower_id: Any, at: datetime, status: str = "pending") -> CommandRecord:
    return CommandRecord(
        command_id=uuid4(),
        account_id=account_id,
        mower_id=mower_id,
        kind="pause_mow",
        command={"kind": "pause_mow"},
        priority="normal",
        issued_by_subject=uuid4(),
        issued_by={"subject": str(uuid4()), "kind": "user"},
        issued_at=at,
        expires_at=at + timedelta(seconds=30),
        status=status,
        correlation_id="c-1",
    )


@pytest.mark.parametrize("kind", STORES)
async def test_fleet_db_store_contract(kind: str, monkeypatch: pytest.MonkeyPatch) -> None:
    acc, other = uuid4(), uuid4()
    async with store_of(kind, monkeypatch) as store:
        async with store.transaction() as tx:
            default = await tx.sites.add(site_row(acc, "Default", default=True))
            home = await tx.sites.add(site_row(acc, "Home"))
            await tx.sites.add(site_row(other, "Home"))  # names are per account
            with pytest.raises(ConflictError):
                await tx.sites.add(site_row(acc, "Other default", default=True))
            with pytest.raises(ConflictError):
                await tx.sites.add(site_row(acc, "HOME"))  # case-insensitive
            await tx.sites.add(site_row(acc, "Default"))  # the default site's name is free
            north = Group(uuid4(), acc, "North", uuid4(), NOW, NOW, site_id=home.id)
            await tx.groups.add(north)
            with pytest.raises(ConflictError):
                await tx.groups.add(Group(uuid4(), acc, "north", uuid4(), NOW, NOW))
            m1 = Mower(uuid4(), acc, home.id, "M1", "simulated", uuid4(), NOW, NOW, serial="S")
            m2 = Mower(uuid4(), acc, default.id, "M2", "simulated", uuid4(), NOW, NOW)
            m3 = Mower(uuid4(), acc, default.id, "M3", "simulated", uuid4(), NOW, NOW)
            for item in (m1, m2, m3):
                await tx.mowers.add(item)
            with pytest.raises(ConflictError):
                await tx.mowers.add(
                    Mower(uuid4(), acc, home.id, "M4", "simulated", uuid4(), NOW, NOW, serial="S")
                )
            await tx.mower_groups.add(MowerGroup(uuid4(), m1.id, north.id, acc))
            with pytest.raises(ConflictError):
                await tx.mower_groups.add(MowerGroup(uuid4(), m1.id, north.id, acc))
            await tx.audit.add(AuditEntry(NOW, "mower.registered", acc, "mower", str(m1.id)))
            entry = await tx.audit.add(AuditEntry(NOW, "mower.removed", acc, "mower", "x"))
        assert entry.id is not None and entry.id >= 2
        async with store.transaction() as tx:
            assert await tx.mowers.count(account_id=acc) == 3
            named = await tx.mowers.find(account_id=acc, order_by="name", descending=True, limit=2)
            assert [m.name for m in named] == ["M3", "M2"]
            some = await tx.mowers.find(id=In([m1.id, m3.id]))
            assert {m.id for m in some} == {m1.id, m3.id}
            assert (await tx.mowers.get(id=m1.id)) == m1
            assert await tx.sites.get(id=home.id, account_id=other) is None
            assert len(await tx.sites.find(created_by=None, account_id=acc)) == 3
            m1.name, m1.connection_options = "Renamed", {"time_scale": 5}
            await tx.mowers.update(m1)
            m2.serial = "S"
            with pytest.raises(ConflictError):
                await tx.mowers.update(m2)  # the savepoint keeps the transaction usable
            for age in (1, 2, 3):
                await tx.commands.add(record(acc, m1.id, NOW - timedelta(days=age)))
        async with store.transaction() as tx:
            assert (await tx.mowers.get(id=m1.id)).connection_options == {"time_scale": 5}  # type: ignore[union-attr]
            old = await tx.commands.find(issued_at=Lt(NOW - timedelta(days=2)))
            recent = await tx.commands.find(issued_at=Gt(NOW - timedelta(days=2)))
            assert (len(old), len(recent)) == (1, 1)
            changed = await tx.commands.update_where(
                {"status": "executed", "result_at": NOW},
                mower_id=m1.id,
                status=In(["pending", "accepted"]),
            )
            again = await tx.commands.update_where(
                {"status": "accepted"}, mower_id=m1.id, status=In(["pending"])
            )
            assert (changed, again) == (3, 0)
            assert await tx.mower_groups.delete(mower_id=m1.id) == 1
            assert await tx.try_lock(42)
        with pytest.raises(RuntimeError):
            async with store.transaction() as tx:
                await tx.mowers.delete(id=m3.id)
                raise RuntimeError("roll back")
        async with store.transaction() as tx:
            assert await tx.mowers.get(id=m3.id) is not None
            assert {c.status for c in await tx.commands.find(account_id=acc)} == {"executed"}


@needs_db
async def test_fleet_db_advisory_lock_admits_one_purger(monkeypatch: pytest.MonkeyPatch) -> None:
    async with store_of("postgres", monkeypatch) as store:
        async with store.transaction() as first, store.transaction() as second:
            assert await first.try_lock(7)
            assert not await second.try_lock(7)
        async with store.transaction() as third:
            assert await third.try_lock(7)  # released with the first transaction


@needs_db
async def test_fleet_db_full_flow(monkeypatch: pytest.MonkeyPatch) -> None:
    async with store_of("postgres", monkeypatch) as store, harness(store=store) as h:
        await fake_gateway(h.bus)
        owner = await h.owner()
        anna = await h.member(owner, "operator")
        home = await h.create(owner, "sites", name="Home", address={"city": "Graz"})
        north = await h.create(owner, "groups", name="North", site_id=home["id"])
        mower_id = (await h.register(owner, site_id=home["id"], group_ids=[north["id"]]))["id"]
        default_mower = await h.register(owner)
        command_id = (
            await h.client.post(
                f"{API}/mowers/{mower_id}/commands",
                headers=anna.headers,
                json={"command": {"kind": "start_mow", "area_ids": ["front"]}},
            )
        ).json()["command_id"]
        record_view = (
            await h.client.get(f"{API}/commands/{command_id}", headers=anna.headers)
        ).json()
        listed = (await h.client.get(f"{API}/mowers", headers=anna.headers)).json()
        deleted = await h.client.delete(f"{API}/mowers/{mower_id}", headers=owner.headers)
        history = await h.client.get(
            f"{API}/mowers/{default_mower['id']}/commands", headers=owner.headers
        )
        purged = await h.app.state.fleet.commands.purge()
    assert record_view["status"] == "executed"
    assert {m["id"] for m in listed["items"]} == {mower_id, default_mower["id"]}
    assert deleted.status_code == 204
    assert history.json()["items"] == []
    assert purged == 0


@pytest.mark.parametrize("kind", STORES)
async def test_fleet_db_drive_sessions_contract(kind: str, monkeypatch: pytest.MonkeyPatch) -> None:
    acc, mower_id = uuid4(), uuid4()
    async with store_of(kind, monkeypatch) as store:
        session = DriveSession(
            id=uuid4(),
            account_id=acc,
            mower_id=mower_id,
            started_by_subject=uuid4(),
            started_by={"subject": str(uuid4()), "kind": "user"},
            started_at=NOW,
            expires_at=NOW + timedelta(minutes=10),
            blade_on_allowed=True,
        )
        async with store.transaction() as tx:
            assert await tx.try_lock(drive_lock_key(mower_id))
            await tx.drive_sessions.add(session)
        async with store.transaction() as tx:
            [found] = await tx.drive_sessions.find(mower_id=mower_id, ended_at=None)
            assert found.blade_on_allowed and found.frames is None
            changed = await tx.drive_sessions.update_where(
                {"ended_at": NOW + timedelta(minutes=1), "end_reason": "client_stop", "frames": 9},
                id=session.id,
                ended_at=None,
            )
            assert changed == 1
        async with store.transaction() as tx:
            assert await tx.drive_sessions.find(mower_id=mower_id, ended_at=None) == []
            stored = await tx.drive_sessions.get(id=session.id)
            assert stored is not None and stored.end_reason == "client_stop"
            assert await tx.drive_sessions.delete(started_at=Lt(NOW + timedelta(seconds=1))) == 1


@needs_db
async def test_fleet_db_migration_0007_area_names_up_and_down(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with temporary_database() as url:
        await migrate(url, monkeypatch, "0006")
        engine = create_async_engine(async_database_url(url))
        try:
            assert "mower_area_names" not in await table_names(engine)
            await migrate(url, monkeypatch, "0007")
            assert "mower_area_names" in await table_names(engine)
            await migrate(url, monkeypatch, "0006", down=True)
            assert "mower_area_names" not in await table_names(engine)
        finally:
            await engine.dispose()


@pytest.mark.parametrize("kind", STORES)
async def test_fleet_db_area_names_contract(kind: str, monkeypatch: pytest.MonkeyPatch) -> None:
    acc = uuid4()
    async with store_of(kind, monkeypatch) as store:
        home = site_row(acc, "Home")
        item = Mower(
            id=uuid4(),
            account_id=acc,
            site_id=home.id,
            name="m",
            connection_kind="simulated",
            created_by=uuid4(),
            created_at=NOW,
            updated_at=NOW,
        )
        async with store.transaction() as tx:
            await tx.sites.add(home)
            await tx.mowers.add(item)
            for area_id, name in (("a1", "Front"), ("a2", "Back")):
                await tx.area_names.add(MowerAreaName(item.id, area_id, acc, name, NOW))
        async with store.transaction() as tx:
            with pytest.raises(ConflictError):
                await tx.area_names.add(MowerAreaName(item.id, "a1", acc, "Again", NOW))
        async with store.transaction() as tx:
            changed = await tx.area_names.update_where(
                {"name": "Yard", "updated_at": NOW + timedelta(seconds=1)},
                mower_id=item.id,
                area_id="a2",
            )
            assert changed == 1
            found = await tx.area_names.find(mower_id=item.id, order_by="area_id")
            assert [(r.area_id, r.name) for r in found] == [("a1", "Front"), ("a2", "Yard")]
            assert await tx.area_names.delete(mower_id=item.id, area_id="a1") == 1
            await tx.mowers.delete(id=item.id)
            if kind == "memory":  # PostgreSQL cascades; the use case deletes explicitly
                await tx.area_names.delete(mower_id=item.id)
        async with store.transaction() as tx:
            assert await tx.area_names.find(account_id=acc) == []


@needs_db
async def test_fleet_db_linked_accounts_and_one_mower_per_iot_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Migration 0008: linked accounts (unique e-mail per account, ignoring case), the
    restricting foreign key from mowers, and one physical mower (IoT id) installation-wide."""

    async with temporary_database() as url:
        await migrate(url, monkeypatch)
        engine = create_async_engine(async_database_url(url))
        store = SqlStore(engine)
        try:
            first, second = uuid4(), uuid4()
            linked = LinkedAccount(
                id=uuid4(),
                account_id=first,
                provider="mammotion",
                email="a@example.com",
                password_encrypted=b"\x01sealed",
                created_at=NOW,
                updated_at=NOW,
                devices=[{"device_name": "Luba-VPA", "is_mower": True}],
            )
            async with store.transaction() as tx:
                site_a = Site(uuid4(), first, "A", "UTC", False, NOW, NOW)
                site_b = Site(uuid4(), second, "B", "UTC", False, NOW, NOW)
                await tx.sites.add(site_a)
                await tx.sites.add(site_b)
                await tx.linked_accounts.add(linked)
                await tx.mowers.add(
                    Mower(
                        uuid4(),
                        first,
                        site_a.id,
                        "m",
                        "cloud",
                        uuid4(),
                        NOW,
                        NOW,
                        linked_account_id=linked.id,
                        iot_id="iot-1",
                    )
                )
            async with store.transaction() as tx:
                copy = LinkedAccount(
                    id=uuid4(),
                    account_id=first,
                    provider="mammotion",
                    email="A@EXAMPLE.COM",
                    password_encrypted=b"x",
                    created_at=NOW,
                    updated_at=NOW,
                )
                with pytest.raises(ConflictError):
                    await tx.linked_accounts.add(copy)
                with pytest.raises(ConflictError):  # same IoT id in another account
                    await tx.mowers.add(
                        Mower(
                            uuid4(),
                            second,
                            site_b.id,
                            "n",
                            "cloud",
                            uuid4(),
                            NOW,
                            NOW,
                            iot_id="iot-1",
                        )
                    )
                found = await tx.linked_accounts.get(id=linked.id)
                assert found is not None and found.password_encrypted == b"\x01sealed"
                assert found.devices == linked.devices
            with pytest.raises(DBAPIError):  # the mower still uses the linked account
                async with engine.begin() as conn:
                    await conn.execute(text("DELETE FROM linked_accounts"))
            await migrate(url, monkeypatch, "0006", down=True)
            assert "linked_accounts" not in await table_names(engine)
        finally:
            await engine.dispose()
