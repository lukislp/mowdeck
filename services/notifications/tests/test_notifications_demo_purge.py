"""Demo sandboxes (ADR 0076): the purge of one expired visitor account.

The API tests run the real app on the memory store; the store test runs on the memory store
and, with ``MOWDECK_TEST_DATABASE_URL``, on PostgreSQL (see ``test_notifications_db``).
"""

from dataclasses import replace
from datetime import timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
from mowdeck_service_kit.demo import DEMO_SEED_HEADER
from notifications_support import API, AUTH, P256DH, T0, harness, make_state, seconds
from test_notifications_db import FENCE, item, message, needs_db, store_of
from test_notifications_flow import finish_a_mow

from notifications.application.ports import Store
from notifications.domain.geofence import MowerFence
from notifications.domain.live_activity import ActivityDevice, ActivityRun, LiveActivity
from notifications.domain.models import (
    ApnsDevice,
    DirectoryEntry,
    MowerWatch,
    NewDelivery,
    NotificationImage,
    NtfyTarget,
    Preferences,
    PushSubscription,
    TargetKind,
)

SEED = "demo-seed-secret"
DEMO: dict[str, Any] = {"demo_mode": True, "demo_seed_token": SEED}


def purge_path(account_id: UUID) -> str:
    return f"{API}/demo/accounts/{account_id}"


# --- the endpoint --------------------------------------------------------------------------


async def test_purge_is_a_demo_only_seed_endpoint() -> None:
    account_id = uuid4()
    async with harness(demo_seed_token=SEED) as h:
        outside = await h.client.delete(purge_path(account_id), headers={DEMO_SEED_HEADER: SEED})
    async with harness(**DEMO) as h:
        without = await h.client.delete(purge_path(account_id))
        wrong = await h.client.delete(purge_path(account_id), headers={DEMO_SEED_HEADER: "nope"})
    assert outside.status_code == 404
    assert without.status_code == 403
    assert wrong.status_code == 403


async def test_purge_removes_one_account_and_keeps_the_other() -> None:
    async with harness(**DEMO) as h:
        gone, kept = await h.owner(), await h.owner()
        mowers: dict[UUID, UUID] = {}
        for owner in (gone, kept):
            mower_id = await h.add_mower(owner)
            mowers[owner.account_id] = mower_id
            await h.subscribe_all(owner)
            await finish_a_mow(h, owner, mower_id)
            await h.publish_state(make_state(mower_id, owner.account_id, at=h.clock.now))
            assert await h.inbox(owner)
        kept_inbox = await h.inbox(kept)
        kept_preferences = await h.client.get(f"{API}/preferences", headers=kept.headers)
        h.services.fanout._photo_log[mowers[gone.account_id]] = [T0]
        h.services.fanout._photo_log[mowers[kept.account_id]] = [T0]

        first = await h.client.delete(purge_path(gone.account_id), headers={DEMO_SEED_HEADER: SEED})
        second = await h.client.delete(
            purge_path(gone.account_id), headers={DEMO_SEED_HEADER: SEED}
        )
        nobody = await h.client.delete(purge_path(uuid4()), headers={DEMO_SEED_HEADER: SEED})

        assert first.status_code == 200, first.text
        assert first.json()["deleted"] >= 5  # directory, watch, preferences, two inbox items
        assert second.status_code == 200
        assert second.json() == {"deleted": 0}
        assert nobody.json() == {"deleted": 0}
        assert await h.inbox(gone) == []
        data = h.store.data
        rows: list[Any] = [
            *data.directory.values(),
            *data.watches.values(),
            *data.geofences.values(),
            *data.preferences.values(),
            *data.push.values(),
            *data.apns.values(),
            *data.ntfy.values(),
            *data.inbox.values(),
            *data.deliveries.values(),
            *data.images.values(),
            *data.activity_devices.values(),
            *data.live_activities.values(),
        ]
        assert all(row.account_id != gone.account_id for row in rows)
        assert all(key[1] != gone.account_id for key in [*data.tests, *data.silences])
        photo_log = h.services.fanout._photo_log
        assert mowers[gone.account_id] not in photo_log
        assert mowers[kept.account_id] in photo_log
        assert await h.inbox(kept) == kept_inbox
        again = await h.client.get(f"{API}/preferences", headers=kept.headers)
        assert again.json() == kept_preferences.json()
        assert data.directory[mowers[kept.account_id]].account_id == kept.account_id


# --- the store (memory and PostgreSQL) -----------------------------------------------------


async def fill(store: Store, account: UUID) -> tuple[UUID, UUID, UUID]:
    """One row in every table of the account; returns the user, the mower and the inbox item.
    15 rows: directory, watch, geofence, preferences, test throttle, push subscription, APNs
    device, ntfy target, inbox item, two deliveries, image, silence, Live Activity device and
    Live Activity."""
    user, mower, installation = uuid4(), uuid4(), uuid4()
    async with store.transaction() as tx:
        await tx.put_directory(DirectoryEntry(mower, account, "A", T0), replace=True)
        await tx.put_watch(MowerWatch(mower, account, T0))
        assert await tx.put_geofence(MowerFence(mower, account, T0, FENCE))
    await store.put_preferences(Preferences(user_id=user, account_id=account, all_mowers=True))
    assert await store.try_test(user, account, T0, timedelta(minutes=1))
    sub = await store.add_push_subscription(
        PushSubscription(uuid4(), user, account, f"https://p.test/{uuid4()}", P256DH, AUTH, T0)
    )
    await store.add_apns_device(ApnsDevice(uuid4(), user, account, uuid4().hex * 2, None, T0))
    await store.put_ntfy_target(NtfyTarget(user, account, "https://ntfy.test", "t", None, T0))
    image = NotificationImage(
        uuid4(), account, mower, T0, T0 + seconds(3600), "image/jpeg", b"\xff\xd8jpeg"
    )
    await store.put_image(image, keep=10)
    inbox_item = replace(item(user, account, "x"), mower_id=mower, image_id=image.id)
    deliveries = [
        NewDelivery(user, account, "webpush", message(), TargetKind.PUSH_SUBSCRIPTION, sub.id),
        NewDelivery(user, account, "log", message()),
    ]
    assert await store.record(inbox_item, deliveries, 200)
    await store.silence(user, account, "theft", T0 + seconds(600))
    await store.put_activity_device(ActivityDevice(user, account, installation, None, True, T0))
    await store.put_live_activity(
        LiveActivity(user, account, installation, mower, ActivityRun.RUNNING, T0)
    )
    return user, mower, inbox_item.id


async def holdings(store: Store, account: UUID, user: UUID, mower: UUID, item_id: UUID) -> Any:
    """What the store hands out about the account (read only)."""
    async with store.transaction() as tx:
        watch = await tx.watch(mower)
    return {
        "directory": await store.directory_entry(mower),
        "watch": watch,
        "geofence": await store.geofence(mower),
        "preferences": await store.preferences(user, account),
        "push": await store.push_subscriptions(user, account),
        "apns": await store.apns_devices(user, account),
        "ntfy": await store.ntfy_target(user, account),
        "inbox": await store.inbox(user, account, 10, unread_only=False),
        "image": await store.inbox_image(user, account, item_id, T0),
        "silenced": await store.silenced(account, "theft", [user], T0),
        "activity_devices": await store.activity_devices(account),
        "live_activities": await store.live_activities(mower),
    }


@pytest.mark.parametrize("kind", ["memory", pytest.param("sql", marks=needs_db)])
async def test_store_purge_deletes_every_table_of_one_account(
    kind: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with store_of(kind, monkeypatch) as store:
        gone, kept = uuid4(), uuid4()
        gone_user, gone_mower, gone_item = await fill(store, gone)
        kept_user, kept_mower, kept_item = await fill(store, kept)
        before = await holdings(store, kept, kept_user, kept_mower, kept_item)
        assert all(before.values())

        result = await store.purge_account(gone)

        assert result.deleted == 15
        assert result.mower_ids == {gone_mower}
        found = await holdings(store, gone, gone_user, gone_mower, gone_item)
        assert all(not value for value in found.values()), found
        assert await store.try_test(gone_user, gone, T0, timedelta(minutes=1))  # throttle gone
        assert await holdings(store, kept, kept_user, kept_mower, kept_item) == before
        assert not await store.try_test(kept_user, kept, T0, timedelta(minutes=1))
        claimed = await store.claim_deliveries(T0, timedelta(seconds=60), 100)
        assert {c.account_id for c in claimed} == {kept} and len(claimed) == 2
        second = await store.purge_account(gone)
        assert second.deleted == 1  # only the throttle row the check above wrote again
        assert (await store.purge_account(gone)).deleted == 0
        assert (await store.purge_account(uuid4())).deleted == 0
