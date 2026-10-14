"""PostgreSQL: migrations up and down, schema drift and the store contract.

The PostgreSQL tests run when ``MOWDECK_TEST_DATABASE_URL`` points at a server where the user
may create databases (CI: ``postgresql://postgres:test@127.0.0.1:5433/postgres``). Each test
creates its own database and drops it afterwards. The store contract tests also run against
the memory store, so both implementations behave the same. Locally::

    docker run -d --name notifications-test-pg -p 5433:5432 -e POSTGRES_PASSWORD=test \\
        public.ecr.aws/docker/library/postgres:17.11-alpine
    MOWDECK_TEST_DATABASE_URL=postgresql://postgres:test@127.0.0.1:5433/postgres \\
        uv run pytest services/notifications -k db
"""

import asyncio
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import time, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from mowdeck_contracts.v1 import MowerActivity, NotificationKind, NotificationSeverity
from mowdeck_service_kit.db import async_database_url
from notifications_support import API, AUTH, P256DH, T0, harness, make_state, seconds
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from notifications.adapters.db.migrate import alembic_config
from notifications.adapters.db.store import SqlStore
from notifications.adapters.db.tables import metadata
from notifications.adapters.memory import MemoryStore
from notifications.application.ports import Store
from notifications.domain.geofence import Fence, MowerFence
from notifications.domain.models import (
    ApnsDevice,
    DeliveryStatus,
    DirectoryEntry,
    InboxItem,
    Message,
    MowerWatch,
    NewDelivery,
    NtfyTarget,
    Preferences,
    PushSubscription,
    QuietHours,
    TargetKind,
)

ADMIN_URL = os.environ.get("MOWDECK_TEST_DATABASE_URL")
needs_db = pytest.mark.skipif(not ADMIN_URL, reason="needs MOWDECK_TEST_DATABASE_URL")


@asynccontextmanager
async def temporary_database() -> AsyncIterator[str]:
    """Create a fresh database and yield its URL; drop it afterwards."""
    assert ADMIN_URL
    name = f"notifications_test_{uuid4().hex[:10]}"
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
async def test_notifications_db_migrations_up_down_and_no_drift(
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


KINDS = ["memory", pytest.param("sql", marks=needs_db)]


def item(user: UUID, account: UUID, dedupe: str, at_seconds: float = 0) -> InboxItem:
    return InboxItem(
        id=uuid4(),
        user_id=user,
        account_id=account,
        mower_id=None,
        kind=NotificationKind.TEST,
        severity=NotificationSeverity.INFO,
        title="t",
        body="b",
        url="/",
        dedupe_key=dedupe,
        occurred_at=T0,
        created_at=T0 + seconds(at_seconds),
    )


def message() -> Message:
    return Message(
        kind=NotificationKind.TEST,
        severity=NotificationSeverity.INFO,
        title="t",
        body="b",
        url=None,
        tag="test",
        mower_id=None,
    )


@pytest.mark.parametrize("kind", KINDS)
async def test_notifications_db_watch_directory_and_offline_candidates(
    kind: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    account, mower, stale, gone = uuid4(), uuid4(), uuid4(), uuid4()
    async with store_of(kind, monkeypatch) as store:
        async with store.transaction() as tx:
            assert await tx.watch(mower) is None
            await tx.put_watch(
                MowerWatch(mower, account, T0, last_sent={"battery_low": T0}, anchor_lat=1.5)
            )
            await tx.put_watch(MowerWatch(stale, account, T0 - seconds(300)))
            await tx.put_watch(MowerWatch(gone, account, T0, disconnected_at=T0 - seconds(60)))
            await tx.put_watch(MowerWatch(uuid4(), account, T0 - seconds(900), offline=True))
            await tx.put_directory(DirectoryEntry(mower, account, "A", T0), replace=False)
            await tx.put_directory(DirectoryEntry(mower, uuid4(), "B", T0), replace=False)
        async with store.transaction() as tx:
            watch = await tx.watch(mower)
            assert watch is not None and watch.last_sent == {"battery_low": T0}
            assert watch.anchor_lat == 1.5
            await tx.put_watch(watch.evolve(activity="mowing"))
            due = await tx.offline_candidates(T0 - seconds(120), T0 - seconds(30), 10)
            assert sorted(str(w.mower_id) for w in due) == sorted([str(stale), str(gone)])
            assert len(await tx.offline_candidates(T0 - seconds(120), T0 - seconds(30), 1)) == 1
        entry = await store.directory_entry(mower)
        assert entry is not None and entry.name == "A" and entry.account_id == account
        async with store.transaction() as tx:
            await tx.put_directory(DirectoryEntry(mower, account, "C", T0), replace=True)
        renamed = await store.directory_entry(mower)
        assert renamed is not None and renamed.name == "C"
        async with store.transaction() as tx:
            stored = await tx.watch(mower)
            assert stored is not None and stored.activity == "mowing"
            await tx.drop_mower(mower)
            assert await tx.watch(mower) is None
        assert await store.directory_entry(mower) is None


@pytest.mark.parametrize("kind", KINDS)
async def test_notifications_db_preferences_and_subscribers(
    kind: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    account, other_account, mower, other_mower = uuid4(), uuid4(), uuid4(), uuid4()
    follower, everything, foreign = uuid4(), uuid4(), uuid4()
    async with store_of(kind, monkeypatch) as store:
        assert await store.preferences(follower, account) is None
        selected = Preferences(
            user_id=follower,
            account_id=account,
            enabled_kinds=frozenset({NotificationKind.BATTERY_LOW}),
            mower_ids=frozenset({mower}),
            quiet_hours=QuietHours(time(22, 0), time(7, 0)),
            timezone="Europe/Berlin",
            language="de",
            channels=frozenset({"ntfy"}),
        )
        await store.put_preferences(selected)
        await store.put_preferences(
            Preferences(user_id=everything, account_id=account, all_mowers=True)
        )
        await store.put_preferences(
            Preferences(user_id=foreign, account_id=other_account, all_mowers=True)
        )
        stored = await store.preferences(follower, account)
        assert stored == selected.evolve(stored=True)
        assert {p.user_id for p in await store.subscribers(account, mower)} == {
            follower,
            everything,
        }
        assert {p.user_id for p in await store.subscribers(account, other_mower)} == {everything}
        await store.put_preferences(selected.evolve(mower_ids=frozenset(), quiet_hours=None))
        changed = await store.preferences(follower, account)
        assert changed is not None and changed.quiet_hours is None
        assert {p.user_id for p in await store.subscribers(account, mower)} == {everything}
        assert await store.try_test(follower, account, T0, timedelta(seconds=60))
        assert not await store.try_test(follower, account, T0 + seconds(59), timedelta(seconds=60))
        assert await store.try_test(follower, account, T0 + seconds(60), timedelta(seconds=60))
        assert await store.try_test(follower, other_account, T0, timedelta(seconds=60))


@pytest.mark.parametrize("kind", KINDS)
async def test_notifications_db_targets_are_per_user(
    kind: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    user, other, account = uuid4(), uuid4(), uuid4()
    async with store_of(kind, monkeypatch) as store:
        first = await store.add_push_subscription(
            PushSubscription(uuid4(), user, account, "https://p.test/1", P256DH, AUTH, T0, "UA")
        )
        moved = await store.add_push_subscription(
            PushSubscription(uuid4(), other, account, "https://p.test/1", P256DH, "x" * 22, T0)
        )
        assert moved.id == first.id and moved.user_id == other and moved.auth == "x" * 22
        assert await store.push_subscriptions(user, account) == []
        assert [s.id for s in await store.push_subscriptions(other, account)] == [first.id]
        assert not await store.delete_push_subscription(user, account, first.id)
        assert await store.delete_push_subscription(other, account, first.id)
        second = await store.add_push_subscription(
            PushSubscription(uuid4(), user, account, "https://p.test/2", P256DH, AUTH, T0)
        )
        await store.remove_push_subscription(second.id)
        assert await store.push_subscriptions(user, account) == []

        assert await store.ntfy_target(user, account) is None
        target = NtfyTarget(user, account, "https://ntfy.test", "lawn", "tk_1", T0)
        await store.put_ntfy_target(target)
        await store.put_ntfy_target(target)
        assert await store.ntfy_target(user, account) == target
        assert await store.ntfy_target(other, account) is None
        assert not await store.delete_ntfy_target(other, account)
        assert await store.delete_ntfy_target(user, account)


@pytest.mark.parametrize("kind", KINDS)
async def test_notifications_db_inbox_dedupe_trim_and_read(
    kind: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    user, other, account = uuid4(), uuid4(), uuid4()
    async with store_of(kind, monkeypatch) as store:
        first = item(user, account, "a")
        assert await store.record(first, [], keep=3)
        assert not await store.record(item(user, account, "a", 1), [], keep=3)  # same key
        assert await store.record(item(other, account, "a"), [], keep=3)  # another user
        for n in range(4):
            assert await store.record(item(user, account, f"k{n}", n + 1), [], keep=3)
        items = await store.inbox(user, account, 10, unread_only=False)
        assert [i.dedupe_key for i in items] == ["k3", "k2", "k1"]
        assert await store.unread_count(user, account) == 3
        read = await store.mark_read(user, account, items[0].id, T0 + seconds(9))
        again = await store.mark_read(user, account, items[0].id, T0 + seconds(99))
        assert read is not None and again is not None and again.read_at == T0 + seconds(9)
        assert await store.mark_read(other, account, items[1].id, T0) is None
        assert await store.mark_read(user, uuid4(), items[1].id, T0) is None
        unread = await store.inbox(user, account, 10, unread_only=True)
        assert [i.dedupe_key for i in unread] == ["k2", "k1"]
        assert await store.unread_count(user, account) == 2
        assert len(await store.inbox(user, account, 1, unread_only=False)) == 1
        assert await store.inbox(user, uuid4(), 10, unread_only=False) == []


@pytest.mark.parametrize("kind", KINDS)
async def test_notifications_db_outbox_claim_finish_prune_and_drop_user(
    kind: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    user, other, account = uuid4(), uuid4(), uuid4()
    async with store_of(kind, monkeypatch) as store:
        sub = await store.add_push_subscription(
            PushSubscription(uuid4(), user, account, "https://p.test/1", P256DH, AUTH, T0)
        )
        await store.put_ntfy_target(NtfyTarget(user, account, "https://ntfy.test", "t", None, T0))
        deliveries = [
            NewDelivery(user, account, "webpush", message(), TargetKind.PUSH_SUBSCRIPTION, sub.id),
            NewDelivery(user, account, "ntfy", message(), TargetKind.NTFY),
            NewDelivery(user, account, "log", message()),
            NewDelivery(user, account, "webpush", message(), TargetKind.PUSH_SUBSCRIPTION, uuid4()),
        ]
        assert await store.record(item(user, account, "x"), deliveries, 200)
        assert await store.record(
            item(other, account, "x"), [NewDelivery(other, account, "log", message())], 200
        )
        claimed = await store.claim_deliveries(T0, timedelta(seconds=60), 10)
        assert len(claimed) == 5 and {c.attempts for c in claimed} == {1}
        assert await store.claim_deliveries(T0 + seconds(59), timedelta(seconds=60), 10) == []
        targets = {
            (c.channel, c.target_id == sub.id): c.target for c in claimed if c.user_id == user
        }
        assert targets[("webpush", True)] == sub.subscription_info()
        assert targets[("webpush", False)] is None  # unknown subscription
        assert targets[("ntfy", False)] == {
            "server_url": "https://ntfy.test",
            "topic": "t",
            "token": None,
        }
        assert targets[("log", False)] is None
        assert claimed[0].message == message()
        by_channel = {
            c.channel + str(c.target_id == sub.id): c for c in claimed if c.user_id == user
        }
        await store.finish_delivery(by_channel["logFalse"].id, DeliveryStatus.SENT, now=T0)
        await store.finish_delivery(
            by_channel["ntfyFalse"].id,
            DeliveryStatus.PENDING,
            now=T0,
            error="503",
            next_attempt_at=T0 + seconds(30),
        )
        retried = await store.claim_deliveries(T0 + seconds(30), timedelta(seconds=60), 10)
        assert [r.channel for r in retried] == ["ntfy"] and retried[0].attempts == 2
        assert await store.prune_deliveries(T0 + seconds(1)) == 1  # the sent one
        await store.drop_user(user, uuid4())  # another account: nothing happens
        assert await store.preferences(user, account) is None
        assert len(await store.inbox(user, account, 10, unread_only=False)) == 1
        await store.drop_user(user, account)
        assert await store.inbox(user, account, 10, unread_only=False) == []
        assert await store.push_subscriptions(user, account) == []
        assert await store.ntfy_target(user, account) is None
        assert len(await store.inbox(other, account, 10, unread_only=False)) == 1
        later = await store.claim_deliveries(T0 + seconds(3600), timedelta(seconds=60), 10)
        assert [c.user_id for c in later] == [other]
        await store.drop_user(other, None)
        assert await store.inbox(other, account, 10, unread_only=False) == []


@pytest.mark.parametrize("kind", KINDS)
async def test_notifications_db_rolls_back_a_failed_transaction(
    kind: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    account, mower = uuid4(), uuid4()
    async with store_of(kind, monkeypatch) as store:
        with pytest.raises(RuntimeError, match="boom"):
            async with store.transaction() as tx:
                await tx.put_watch(MowerWatch(mower, account, T0))
                raise RuntimeError("boom")
        async with store.transaction() as tx:
            assert await tx.watch(mower) is None


@needs_db
async def test_notifications_db_full_flow_over_http_with_postgres(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with temporary_database() as url:
        await migrate(url, monkeypatch)
        engine = create_async_engine(async_database_url(url))
        try:
            async with harness(store=SqlStore(engine)) as h:
                owner = await h.owner()
                mower_id = await h.add_mower(owner, name="Vorgarten")
                await h.subscribe_all(owner, language="de")
                created = await h.client.post(
                    f"{API}/push-subscriptions",
                    json={
                        "endpoint": "https://push.example.test/a",
                        "keys": {"p256dh": P256DH, "auth": AUTH},
                    },
                    headers=owner.headers,
                )
                assert created.status_code == 201
                for delta, activity in (
                    (0, MowerActivity.DOCKED),
                    (10, MowerActivity.MOWING),
                    (900, MowerActivity.CHARGING),
                ):
                    h.clock.advance(delta)
                    await h.publish_state(
                        make_state(mower_id, owner.account_id, at=h.clock.now, activity=activity)
                    )
                await h.services.outbox.run_once()
                items = await h.inbox(owner)
                intruder = await h.owner()
                await h.add_mower(intruder)
                foreign = await h.client.post(
                    f"{API}/inbox/{items[0]['id']}/read", headers=intruder.headers
                )
        finally:
            await engine.dispose()
    assert [i["kind"] for i in items] == ["mowing_finished", "job_started"]
    assert items[0]["title"] == "Vorgarten ist fertig mit Mähen"
    assert len(h.channel.sent) == 2 and foreign.status_code == 404


FENCE = Fence(
    origin_lat=48.1,
    origin_lon=11.5,
    rotation_deg=12.5,
    areas=(((0.0, 0.0), (12.0, 0.0), (12.0, 8.0), (0.0, 8.0)),),
    paths=(((1.0, 8.0), (1.0, 11.0)),),
    dock=(-1.0, 1.0),
)


@pytest.mark.parametrize("kind", KINDS)
async def test_notifications_theft_db_geofence_newest_wins_and_drop(
    kind: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    account, mower = uuid4(), uuid4()
    async with store_of(kind, monkeypatch) as store:
        assert await store.geofence(mower) is None
        newer = MowerFence(mower, account, T0 + seconds(60), FENCE)
        async with store.transaction() as tx:
            assert await tx.put_geofence(newer)
            older = MowerFence(mower, account, T0, Fence(1.0, 2.0, 0.0, areas=FENCE.areas))
            assert not await tx.put_geofence(older)
        assert await store.geofence(mower) == newer
        bare = MowerFence(mower, account, T0 + seconds(60), Fence(1.0, 2.0, 0.0, FENCE.areas))
        async with store.transaction() as tx:
            assert await tx.put_geofence(bare)  # the same time replaces
        assert await store.geofence(mower) == bare
        async with store.transaction() as tx:
            await tx.put_watch(
                MowerWatch(
                    mower,
                    account,
                    T0,
                    last_command_at=T0,
                    last_drive_at=T0 - seconds(5),
                    left_dock_at=T0 + seconds(1),
                    outside_count=2,
                    geofence_notified=True,
                )
            )
        async with store.transaction() as tx:
            watch = await tx.watch(mower)
            assert watch is not None and watch.outside_count == 2 and watch.geofence_notified
            assert watch.last_drive_at == T0 - seconds(5) and watch.left_dock_at == T0 + seconds(1)
            await tx.drop_mower(mower)
        assert await store.geofence(mower) is None


@pytest.mark.parametrize("kind", KINDS)
async def test_notifications_theft_db_acknowledge_and_silences(
    kind: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    user, other, account = uuid4(), uuid4(), uuid4()
    async with store_of(kind, monkeypatch) as store:
        first = item(user, account, "a")
        assert await store.record(first, [], keep=10)
        assert await store.acknowledge(other, account, first.id, T0) is None
        assert await store.acknowledge(user, uuid4(), first.id, T0) is None
        acked = await store.acknowledge(user, account, first.id, T0 + seconds(5))
        assert acked is not None and acked.acked_at == T0 + seconds(5) == acked.read_at
        again = await store.acknowledge(user, account, first.id, T0 + seconds(50))
        assert again is not None and again.acked_at == T0 + seconds(5)
        second = item(user, account, "b")
        assert await store.record(second, [], keep=10)
        await store.mark_read(user, account, second.id, T0)
        late = await store.acknowledge(user, account, second.id, T0 + seconds(9))
        assert late is not None and late.read_at == T0 and late.acked_at == T0 + seconds(9)
        listed = await store.inbox(user, account, 10, unread_only=False)
        assert {i.acked_at for i in listed} == {T0 + seconds(5), T0 + seconds(9)}

        key = f"mower_lifted:{uuid4()}"
        assert await store.silenced(account, key, [], T0) == set()
        assert await store.silenced(account, key, [user, other], T0) == set()
        await store.silence(user, account, key, T0 + seconds(3600))
        await store.silence(user, account, key, T0 + seconds(60))  # never shortened
        await store.silence(other, uuid4(), key, T0 + seconds(3600))  # another account
        assert await store.silenced(account, key, [user, other], T0) == {user}
        assert await store.silenced(account, key, [user], T0 + seconds(3599)) == {user}
        assert await store.silenced(account, key, [user], T0 + seconds(3600)) == set()
        assert await store.silenced(account, "geofence_exit:x", [user], T0) == set()
        await store.silence(user, account, key, T0 + seconds(7200))  # extended
        assert await store.silenced(account, key, [user], T0 + seconds(5000)) == {user}
        await store.drop_user(user, account)
        assert await store.silenced(account, key, [user], T0) == set()


@pytest.mark.parametrize("kind", KINDS)
async def test_notifications_apns_db_devices_claims_and_drop_user(
    kind: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    user, other, account = uuid4(), uuid4(), uuid4()
    token, second_token = "a1" * 32, "b2" * 40
    async with store_of(kind, monkeypatch) as store:
        first = await store.add_apns_device(ApnsDevice(uuid4(), user, account, token, "Old", T0))
        moved = await store.add_apns_device(
            ApnsDevice(uuid4(), other, account, token, "New", T0 + seconds(9))
        )
        assert moved.id == first.id and moved.user_id == other and moved.device_name == "New"
        assert moved.created_at == T0  # the registration time is kept
        assert await store.apns_devices(user, account) == []
        later = await store.add_apns_device(
            ApnsDevice(uuid4(), other, account, second_token, None, T0 + seconds(5))
        )
        assert [d.id for d in await store.apns_devices(other, account)] == [first.id, later.id]
        assert await store.apns_devices(other, uuid4()) == []
        assert not await store.delete_apns_device(user, account, first.id)
        assert not await store.delete_apns_device(other, uuid4(), first.id)
        assert await store.delete_apns_device(other, account, first.id)
        assert not await store.delete_apns_device(other, account, first.id)
        await store.remove_apns_device(later.id)
        await store.remove_apns_device(uuid4())  # unknown: nothing happens
        assert await store.apns_devices(other, account) == []

        device = await store.add_apns_device(ApnsDevice(uuid4(), user, account, token, None, T0))
        deliveries = [
            NewDelivery(user, account, "apns", message(), TargetKind.APNS_DEVICE, device.id),
            NewDelivery(user, account, "apns", message(), TargetKind.APNS_DEVICE, uuid4()),
        ]
        assert await store.record(item(user, account, "x"), deliveries, 200)
        foreign = NewDelivery(other, account, "apns", message(), TargetKind.APNS_DEVICE, device.id)
        assert await store.record(item(other, account, "x"), [foreign], 200)
        claimed = await store.claim_deliveries(T0, timedelta(seconds=60), 10)
        targets = {(c.user_id, c.target_id): c.target for c in claimed}
        assert targets[(user, device.id)] == {"token": token}
        assert targets[(other, device.id)] is None  # not the other user's device
        assert sorted(t is None for t in targets.values()) == [False, True, True]
        await store.drop_user(user, uuid4())  # another account: nothing happens
        assert len(await store.apns_devices(user, account)) == 1
        await store.drop_user(user, account)
        assert await store.apns_devices(user, account) == []


@needs_db
async def test_notifications_apns_db_migration_adds_and_removes_the_channel(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with temporary_database() as url:
        await migrate(url, monkeypatch, "0004")
        engine = create_async_engine(async_database_url(url))
        try:
            async with engine.begin() as conn:
                for channels in ('["webpush", "ntfy"]', '["apns", "log"]', "[]"):
                    await conn.execute(
                        text(
                            "INSERT INTO preferences (user_id, account_id, enabled_kinds,"
                            " all_mowers, mower_ids, timezone, language, channels, updated_at)"
                            " VALUES (:user, :account, '[]', true, '[]', 'UTC', 'en',"
                            " CAST(:channels AS jsonb), now())"
                        ),
                        {"user": uuid4(), "account": uuid4(), "channels": channels},
                    )

            async def stored_channels() -> list[list[str]]:
                async with engine.connect() as conn:
                    rows = await conn.execute(
                        text(
                            "SELECT channels FROM preferences ORDER BY jsonb_array_length(channels)"
                        )
                    )
                    return sorted(sorted(row[0]) for row in rows)

            await migrate(url, monkeypatch, "0005")
            assert "apns_devices" in await table_names(engine)
            assert await stored_channels() == [
                ["apns"],
                ["apns", "log"],
                ["apns", "ntfy", "webpush"],
            ]
            await migrate(url, monkeypatch, "0004", down=True)
            assert "apns_devices" not in await table_names(engine)
            assert await stored_channels() == [[], ["log"], ["ntfy", "webpush"]]
        finally:
            await engine.dispose()
