# SPDX-FileCopyrightText: 2026 Lukas Körber
# SPDX-License-Identifier: AGPL-3.0-only

"""A deleted user (ADR 0082, "delete me"): on ``identity.user.deleted`` Notifications deletes
everything that is the person's, the camera photos only their inbox showed included, and keeps
what is the account's and the other members'.

The event goes over the in-memory bus to the real app, as Identity publishes it. The store is the
memory store and, with ``MOWDECK_TEST_DATABASE_URL``, PostgreSQL (see ``test_notifications_db``).
"""

import asyncio
from dataclasses import replace
from typing import Any
from uuid import UUID, uuid4

import pytest
from mowdeck_contracts.v1 import IDENTITY_USER_DELETED, EventEnvelope, UserChanged
from mowdeck_contracts.v1.subjects import event_subject_for_type
from mowdeck_service_kit.bus import InMemoryBus
from mowdeck_service_kit.db import principal_ref_columns, user_tables
from mowdeck_service_kit.user_erasure import UserErasureIncomplete
from notifications_support import T0, harness, seconds
from test_notifications_db import KINDS, item, message, store_of
from test_notifications_demo_purge import fill, holdings

from notifications.adapters.bus.user_erasure import USERS_CONSUMER
from notifications.adapters.db import tables
from notifications.adapters.db.store import SqlStore
from notifications.adapters.db.tables import metadata
from notifications.adapters.memory import MemoryStore
from notifications.application.ports import Store
from notifications.domain.models import (
    Delivery,
    DeliveryOutcome,
    DeliveryResult,
    NewDelivery,
    NotificationImage,
    Preferences,
)

SUBJECT = event_subject_for_type(IDENTITY_USER_DELETED)

USER_TABLES = {
    "alarm_silences",
    "apns_devices",
    "deliveries",
    "inbox",
    "live_activities",
    "live_activity_devices",
    "ntfy_targets",
    "preferences",
    "push_subscriptions",
    "test_requests",
}
"""The tables of a user's own rows, as the schema gives them (``user_id`` column)."""

MEMORY_USER_TABLES = {
    "activity_devices",
    "apns",
    "deliveries",
    "inbox",
    "live_activities",
    "ntfy",
    "preferences",
    "push",
    "silences",
    "tests",
}
"""The same tables in the memory store, under its names."""


def user_table_names(store: Store) -> set[str]:
    return USER_TABLES if isinstance(store, SqlStore) else MEMORY_USER_TABLES


async def deleted(bus: InMemoryBus, user_id: UUID, account_id: UUID | None) -> None:
    """What Identity publishes when a user is deleted."""
    payload = UserChanged(user_id=user_id, account_id=account_id, reason="self")
    await bus.publish(
        SUBJECT,
        EventEnvelope[UserChanged](
            type=IDENTITY_USER_DELETED,
            version=2,
            producer="identity",
            account_id=account_id,
            payload=payload,
        ),
    )


async def shared_photo(store: Store, account: UUID, mower: UUID, *users: UUID) -> UUID:
    """One camera photo of an event, shown by an inbox item of each of ``users``."""
    photo = NotificationImage(
        uuid4(), account, mower, T0, T0 + seconds(3600), "image/jpeg", b"\xff\xd8shared"
    )
    await store.put_image(photo, keep=10)
    for user in users:
        shown = replace(item(user, account, f"shared-{photo.id}"), image_id=photo.id)
        assert await store.record(shown, [], 200)
    return photo.id


async def own_holdings(
    store: Store, account: UUID, user: UUID, mower: UUID, item_id: UUID
) -> dict[str, Any]:
    """What the store hands out about a user and their mower (the account-wide list of Live
    Activity devices narrowed to the user)."""
    found = dict(await holdings(store, account, user, mower, item_id))
    found["activity_devices"] = [d for d in found["activity_devices"] if d.user_id == user]
    return found


async def photo_ids(store: Store) -> set[UUID]:
    """The ids of every stored camera photo."""
    if isinstance(store, MemoryStore):
        return set(store.data.images)
    assert isinstance(store, SqlStore)
    async with store.engine.connect() as conn:
        return {row.id for row in await conn.execute(tables.notification_images.select())}


# --- the schema ------------------------------------------------------------------------------


def test_notifications_keeps_these_tables_of_a_users_own_rows() -> None:
    """The erasure deletes every table with a ``user_id`` column, read from the schema. This
    fails when one is added or removed, so the README's list stays true."""
    assert {table.name for table in user_tables(metadata)} == USER_TABLES


def test_notifications_stores_no_persons_name() -> None:
    """No principal reference: nothing has to be scrubbed from rows that stay."""
    assert principal_ref_columns(metadata) == ()


# --- the event -------------------------------------------------------------------------------


@pytest.mark.parametrize("kind", KINDS)
async def test_a_deleted_user_leaves_nothing_and_the_others_keep_everything(
    kind: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    mine, other = uuid4(), uuid4()
    async with store_of(kind, monkeypatch) as store, harness(store=store) as h:
        anna, annas_mower, annas_item = await fill(store, mine)
        ben, bens_mower, bens_item = await fill(store, mine)
        carl, carls_mower, carls_item = await fill(store, other)
        own = (await holdings(store, mine, anna, annas_mower, annas_item))["image"].id
        both = await shared_photo(store, mine, annas_mower, anna, ben)
        photos_before = await photo_ids(store)
        before = await store.user_rows(anna)
        # The seed reaches every table of a user's own rows, so none is checked empty.
        assert set(before) == user_table_names(store)
        account_before = await store.account_rows(mine)
        kept = {
            "ben": await own_holdings(store, mine, ben, bens_mower, bens_item),
            "carl": await own_holdings(store, other, carl, carls_mower, carls_item),
            "ben_rows": await store.user_rows(ben),
            "carl_rows": await store.user_rows(carl),
            "other_account": await store.account_rows(other),
        }

        await deleted(h.bus, anna, mine)
        await deleted(h.bus, anna, mine)  # a redelivered event changes nothing

        after = await store.user_rows(anna)
        again = await store.forget_user(anna, mine)
        annas = await holdings(store, mine, anna, annas_mower, annas_item)
        account_after = await store.account_rows(mine)
        photos_after = await photo_ids(store)
        still = {
            "ben": await own_holdings(store, mine, ben, bens_mower, bens_item),
            "carl": await own_holdings(store, other, carl, carls_mower, carls_item),
            "ben_rows": await store.user_rows(ben),
            "carl_rows": await store.user_rows(carl),
            "other_account": await store.account_rows(other),
        }
    assert after == {} and again == 0
    for name in ("preferences", "push", "apns", "ntfy", "inbox", "image", "silenced"):
        assert not annas[name], name
    assert not [a for a in annas["activity_devices"] if a.user_id == anna]
    assert not [a for a in annas["live_activities"] if a.user_id == anna]
    # The account's rows stay: directory, watch and geofence of the mower, and the photo that
    # Ben's item still shows; the photo only Anna's item showed is gone.
    assert annas["directory"] is not None and annas["watch"] is not None
    assert annas["geofence"] is not None
    assert photos_before - photos_after == {own}
    assert both in photos_after
    gone = sum(before.values()) + 1  # her rows (the shared item counted) and her own photo
    assert sum(account_before.values()) - sum(account_after.values()) == gone
    assert still == kept
    assert h.bus.dropped == []


@pytest.mark.parametrize("kind", KINDS)
async def test_the_account_of_the_event_bounds_the_deletion(
    kind: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    mine, other = uuid4(), uuid4()
    async with store_of(kind, monkeypatch) as store:
        anna, _, _ = await fill(store, mine)
        await store.put_preferences(Preferences(user_id=anna, account_id=other, all_mowers=True))
        in_account = await store.forget_user(anna, mine)
        left = await store.user_rows(anna)
        everywhere = await store.forget_user(anna, None)
        none = await store.user_rows(anna)
    assert in_account >= len(user_table_names(store))
    assert left == {"preferences": 1} and everywhere == 1 and none == {}


class FlakyStore(MemoryStore):
    """A store whose first deletion of a user deletes nothing."""

    def __init__(self) -> None:
        super().__init__()
        self.calls = 0

    async def forget_user(self, user_id: UUID, account_id: UUID | None) -> int:
        self.calls += 1
        if self.calls == 1:
            return 0
        return await super().forget_user(user_id, account_id)


async def test_rows_that_remain_fail_the_event_and_a_redelivery_finishes() -> None:
    """A deletion that leaves a row behind is never acknowledged as done."""
    store = FlakyStore()
    async with harness(store=store) as h:
        mine = uuid4()
        anna, _, _ = await fill(store, mine)
        erasure = h.services.user_erasure
        assert erasure is not None
        with pytest.raises(UserErasureIncomplete) as raised:
            await erasure.erase(anna, mine)
        assert set(raised.value.leftovers) == MEMORY_USER_TABLES
        store.calls = 0
        await deleted(h.bus, anna, mine)  # fails once, the bus delivers it again
        left = await store.user_rows(anna)
    assert store.calls == 2
    assert left == {} and h.bus.dropped == []


class BlockingChannel:
    """A channel whose sends hang until released; it records whom it sent to and cancelled."""

    name = "log"

    def __init__(self) -> None:
        self.release = asyncio.Event()
        self.started: list[UUID] = []
        self.sent: list[UUID] = []
        self.cancelled: list[UUID] = []

    async def send(self, delivery: Delivery) -> DeliveryResult:
        self.started.append(delivery.user_id)
        try:
            await self.release.wait()
        except asyncio.CancelledError:
            self.cancelled.append(delivery.user_id)
            raise
        self.sent.append(delivery.user_id)
        return DeliveryResult(DeliveryOutcome.SENT)


async def test_the_sends_the_outbox_already_claimed_for_the_user_are_cancelled() -> None:
    """A delivery claimed before the event is in memory only: its send to the deleted user is
    cancelled, the other member's goes on."""
    account = uuid4()
    anna, ben = uuid4(), uuid4()
    channel = BlockingChannel()
    async with harness(channels={"log": channel}) as h:
        for user in (anna, ben):
            delivery = NewDelivery(user, account, "log", message())
            assert await h.store.record(item(user, account, "x"), [delivery], 200)
        assert await h.services.outbox.dispatch() == 2
        for _ in range(5):
            await asyncio.sleep(0)
        assert sorted(channel.started, key=str) == sorted([anna, ben], key=str)

        await deleted(h.bus, anna, account)
        channel.release.set()
        await h.services.outbox.drain()

        assert channel.cancelled == [anna]
        assert channel.sent == [ben]
        assert await h.store.user_rows(anna) == {}
        assert h.services.outbox._sending == {}


async def test_a_user_notifications_never_saw_and_a_malformed_event_are_no_error() -> None:
    async with harness() as h:
        await deleted(h.bus, uuid4(), uuid4())
        await h.bus.publish(SUBJECT, b'{"type": "identity.user.deleted", "payload": {}}')
        drops: list[Any] = [m for m in h.bus.dropped if m.subject == SUBJECT]
    # Dropped once per consumer (the user erasure's and the identity events'), never retried.
    assert len(drops) == 2


def test_the_consumer_is_durable_and_shared_by_the_replicas() -> None:
    assert USERS_CONSUMER == "notifications-users"
