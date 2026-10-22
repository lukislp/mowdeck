"""Consumers, rules and fan-out together: states in, inbox items and deliveries out."""

import asyncio
from typing import Any
from uuid import uuid4

from mowdeck_contracts.v1 import (
    IDENTITY_USER_CREATED,
    IDENTITY_USER_DELETED,
    IDENTITY_USER_DISABLED,
    ConnectionKind,
    EventEnvelope,
    MowerActivity,
    MowerListReply,
    MowerRegistration,
    NotificationKind,
    NotificationSent,
    UserChanged,
)
from mowdeck_contracts.v1.subjects import FLEET_MOWERS_LIST, telemetry_subject
from mowdeck_service_kit.bus import BusMessage, publish_event
from notifications_support import (
    API,
    AUTH,
    P256DH,
    Harness,
    Person,
    RecordingChannel,
    harness,
    make_state,
)

from notifications.adapters.bus.fleet import seed_directory
from notifications.domain.models import ApnsDevice, DeliveryStatus, NotificationEvent, TargetKind


async def finish_a_mow(h: Harness, owner: Person, mower_id: Any) -> None:
    """Docked, ten minutes of mowing, docked again."""
    states = [
        (0, MowerActivity.DOCKED, 90),
        (10, MowerActivity.MOWING, 88),
        (600, MowerActivity.RETURNING, 60),
        (60, MowerActivity.CHARGING, 58),
    ]
    for delta, activity, battery in states:
        h.clock.advance(delta)
        await h.publish_state(
            make_state(
                mower_id, owner.account_id, at=h.clock.now, activity=activity, battery=battery
            )
        )


def sent_events(h: Harness) -> list[NotificationSent]:
    return [
        m.model(EventEnvelope[NotificationSent]).payload
        for m in h.bus.published
        if m.subject == "event.notifications.sent"
    ]


async def test_notifications_flow_mowing_finished_reaches_inbox_channel_and_bus() -> None:
    async with harness() as h:
        owner = await h.owner()
        mower_id = await h.add_mower(owner, name="Backyard")
        await h.subscribe_all(owner, language="de")
        await finish_a_mow(h, owner, mower_id)
        items = await h.inbox(owner)
        await h.services.outbox.run_once()
    kinds = [i["kind"] for i in items]
    assert kinds == ["mowing_finished", "job_started"]
    finished = items[0]
    assert finished["title"] == "Backyard ist fertig mit Mähen"
    assert finished["body"] == "11 Min. gemäht. Akku 58 %."
    assert finished["url"] == f"/mowers/{mower_id}" and finished["read"] is False
    assert [d.message.kind.value for d in h.channel.sent] == ["job_started", "mowing_finished"]
    assert all(d.user_id == owner.user_id for d in h.channel.sent)
    events = sent_events(h)
    assert [e.kind.value for e in events] == ["job_started", "mowing_finished"]
    assert events[-1].channels == ["log"] and events[-1].user_id == owner.user_id


async def test_notifications_flow_only_subscribers_with_the_kind_get_it() -> None:
    async with harness() as h:
        owner = await h.owner()
        mower_id = await h.add_mower(owner)
        other_mower = await h.add_mower(owner)
        follower = await h.member(owner)
        await h.grant_view(follower, mower_id)
        await h.grant_view(follower, other_mower)
        response = await h.client.put(
            f"{API}/preferences",
            json={"enabled_kinds": ["battery_low"], "mower_ids": [str(other_mower)]},
            headers=follower.headers,
        )
        assert response.status_code == 200, response.text
        await h.subscribe_all(owner, enabled_kinds=["mower_stuck"])
        await finish_a_mow(h, owner, mower_id)
        assert await h.inbox(owner) == [] and await h.inbox(follower) == []
        h.clock.advance(10)
        await h.publish_state(make_state(other_mower, owner.account_id, at=h.clock.now, battery=50))
        h.clock.advance(10)
        await h.publish_state(
            make_state(
                other_mower,
                owner.account_id,
                at=h.clock.now,
                battery=10,
                activity=MowerActivity.IDLE,
            )
        )
        follower_items = await h.inbox(follower)
        assert [i["kind"] for i in follower_items] == ["battery_low"]
        assert await h.inbox(owner) == []


async def test_notifications_flow_revoked_viewer_gets_nothing() -> None:
    async with harness() as h:
        owner = await h.owner()
        mower_id = await h.add_mower(owner)
        viewer = await h.member(owner)
        await h.grant_view(viewer, mower_id)
        await h.subscribe_all(viewer)
        await h.subscribe_all(owner)
        await h.grant_view(viewer, mower_id, revoke=True)
        await finish_a_mow(h, owner, mower_id)
        assert await h.store.inbox(viewer.user_id, viewer.account_id, 10, unread_only=False) == []
        assert len(await h.inbox(owner)) == 2
        # without any visible mower the viewer cannot use the API at all
        response = await h.client.get(f"{API}/inbox", headers=viewer.headers)
        assert response.status_code == 403
        checks = [c for c in h.authz.checks if str(viewer.user_id) in str(c)]
        assert checks  # can_view was asked for the viewer


async def test_notifications_flow_quiet_hours_keep_inbox_but_skip_channels() -> None:
    async with harness() as h:
        owner = await h.owner()
        mower_id = await h.add_mower(owner)
        # T0 is 12:00 UTC = 14:00 in Berlin
        await h.subscribe_all(
            owner, timezone="Europe/Berlin", quiet_hours={"start": "13:30", "end": "15:00"}
        )
        await finish_a_mow(h, owner, mower_id)
        assert len(await h.inbox(owner)) == 2
        assert await h.services.outbox.run_once() == 0
        assert sent_events(h)[-1].channels == []


async def test_notifications_flow_webpush_and_ntfy_targets_get_deliveries() -> None:
    webpush, ntfy = RecordingChannel("webpush"), RecordingChannel("ntfy")
    async with harness(channels={"webpush": webpush, "ntfy": ntfy}) as h:
        owner = await h.owner()
        mower_id = await h.add_mower(owner)
        await h.subscribe_all(owner, enabled_kinds=["battery_low"])
        for endpoint in ("https://push.example.test/a", "https://push.example.test/b"):
            created = await h.client.post(
                f"{API}/push-subscriptions",
                json={"endpoint": endpoint, "keys": {"p256dh": P256DH, "auth": AUTH}},
                headers=owner.headers,
            )
            assert created.status_code == 201, created.text
        await h.client.put(
            f"{API}/ntfy",
            json={"server_url": "https://ntfy.example.test", "topic": "lawn"},
            headers=owner.headers,
        )
        for battery in (50, 10):
            h.clock.advance(10)
            await h.publish_state(
                make_state(mower_id, owner.account_id, at=h.clock.now, battery=battery)
            )
        await h.services.outbox.run_once()
    assert len(webpush.sent) == 2 and len(ntfy.sent) == 1
    assert {d.target["endpoint"] for d in webpush.sent if d.target} == {
        "https://push.example.test/a",
        "https://push.example.test/b",
    }
    assert ntfy.sent[0].target == {
        "server_url": "https://ntfy.example.test",
        "topic": "lawn",
        "token": None,
    }
    assert webpush.sent[0].target_kind is TargetKind.PUSH_SUBSCRIPTION


async def test_notifications_flow_offline_timer_and_connection_events() -> None:
    async with harness(notifications_disconnect_grace_seconds=30) as h:
        owner = await h.owner()
        mower_id = await h.add_mower(owner)
        await h.subscribe_all(owner)
        await h.publish_state(make_state(mower_id, owner.account_id, at=h.clock.now))
        h.clock.advance(60)
        assert await h.services.watcher.sweep_offline() == []
        h.clock.advance(60)
        swept = await h.services.watcher.sweep_offline()
        assert [e.kind.value for e in swept] == ["mower_offline"]
        h.clock.advance(5)
        await h.link(mower_id, owner.account_id, connected=True)
        h.clock.advance(400)
        await h.publish_state(make_state(mower_id, owner.account_id, at=h.clock.now))
        await h.link(mower_id, owner.account_id, connected=False)
        h.clock.advance(31)
        swept = await h.services.watcher.sweep_offline()
        assert [e.kind.value for e in swept] == ["mower_offline"]
        items = await h.inbox(owner)
    assert [i["kind"] for i in items] == ["mower_offline", "mower_online", "mower_offline"]
    assert items[0]["body"] == "The connection to the mower was lost."
    assert items[2]["body"] == "No report for 2 min."


async def test_notifications_flow_foreign_or_malformed_messages_are_dropped() -> None:
    async with harness() as h:
        owner = await h.owner()
        mower_id = await h.add_mower(owner)
        await h.subscribe_all(owner)
        await h.publish_state(make_state(mower_id, uuid4(), at=h.clock.now))  # wrong account
        await h.bus.publish(telemetry_subject(uuid4()), make_state(mower_id, owner.account_id))
        await h.bus.publish(telemetry_subject(mower_id), b"{}")
        await h.bus.publish("event.mower.registered", b"nope")
        await h.bus.publish(
            "event.mower.registered",
            EventEnvelope[dict[str, Any]](
                type="mower.registered", producer="fleet", payload={"x": 1}
            ),
        )
        await h.bus.publish(
            "event.mower.connected",
            EventEnvelope[dict[str, Any]](type="mower.connected", producer="gw", payload={"x": 1}),
        )
        await h.link(mower_id, uuid4(), connected=False)  # wrong account
        await h.bus.publish("event.identity.user.deleted", b"nope")
        await h.bus.publish(
            "event.identity.user.deleted",
            EventEnvelope[dict[str, Any]](
                type=IDENTITY_USER_DELETED, producer="identity", payload={"x": 1}
            ),
        )
        assert len(h.bus.dropped) == 9
        assert await h.inbox(owner) == []


async def test_notifications_flow_user_removed_and_mower_removed() -> None:
    async with harness() as h:
        owner = await h.owner()
        mower_id = await h.add_mower(owner)
        member = await h.member(owner)
        await h.grant_view(member, mower_id)
        await h.subscribe_all(member)
        await h.subscribe_all(owner)
        await finish_a_mow(h, owner, mower_id)
        assert len(await h.inbox(member)) == 2

        async def identity_event(event_type: str, person: Person) -> None:
            await publish_event(
                h.bus,
                EventEnvelope[UserChanged](
                    type=event_type,
                    producer="identity",
                    account_id=person.account_id,
                    payload=UserChanged(
                        user_id=person.user_id, account_id=person.account_id, username="x"
                    ),
                ),
            )

        await identity_event(IDENTITY_USER_CREATED, member)  # ignored
        assert await h.store.preferences(member.user_id, member.account_id) is not None
        await identity_event(IDENTITY_USER_DISABLED, member)
        assert await h.store.preferences(member.user_id, member.account_id) is None
        assert await h.inbox(member) == []
        assert not [d for d in h.store.data.deliveries.values() if d.user_id == member.user_id]
        await identity_event(IDENTITY_USER_DELETED, member)  # idempotent
        await h.announce(mower_id, owner.account_id, removed=True)
        assert await h.store.directory_entry(mower_id) is None
        assert mower_id not in h.store.data.watches


async def test_notifications_flow_dedupe_redelivery_and_unnamed_mower() -> None:
    async with harness() as h:
        owner = await h.owner()
        mower_id = await h.add_mower(owner, announce=False)
        await h.subscribe_all(owner)
        # the same event twice (as after a crash between commit and fan-out) is stored once
        event = NotificationEvent.of(
            NotificationKind.BATTERY_LOW, mower_id, owner.account_id, h.clock.now, "x", battery=9
        )
        assert await h.services.fanout.dispatch([event, event]) == 1
        items = await h.inbox(owner)
    assert len(items) == 1 and items[0]["title"] == "Your mower: battery low"
    assert [d.status for d in h.store.data.deliveries.values()] == [DeliveryStatus.PENDING]


async def test_notifications_flow_directory_seed_from_fleet() -> None:
    async with harness() as h:
        owner = await h.owner()
        mower_id = uuid4()
        registration = MowerRegistration(
            mower_id=mower_id,
            account_id=owner.account_id,
            name="Seeded",
            connection_kind=ConnectionKind.SIMULATED,
        )

        async def answer(message: BusMessage) -> None:
            await message.respond(MowerListReply(mowers=[registration]))

        await h.bus.subscribe(FLEET_MOWERS_LIST, answer)
        seeded: list[int] = []
        count = await seed_directory(
            h.bus,
            h.services.watcher,
            timeout_seconds=1,
            retry_max_seconds=1,
            on_seeded=seeded.append,
            first_delay=0.01,
        )
        entry = await h.store.directory_entry(mower_id)
    assert count == 1 and seeded == [1]
    assert entry is not None and entry.name == "Seeded"


async def test_notifications_flow_seed_retries_until_fleet_answers() -> None:
    async with harness(background_tasks=True) as h:
        for _ in range(100):
            if h.app.state.directory_seeded:
                break
            if _ == 5:

                async def answer(message: BusMessage) -> None:
                    await message.respond(MowerListReply())

                await h.bus.subscribe(FLEET_MOWERS_LIST, answer)
            await asyncio.sleep(0.02)
        assert h.app.state.directory_seeded is True


async def test_notifications_flow_offline_state_after_a_lost_link_is_no_contact() -> None:
    async with harness(notifications_disconnect_grace_seconds=30) as h:
        owner = await h.owner()
        mower_id = await h.add_mower(owner)
        await h.subscribe_all(owner)
        last = make_state(mower_id, owner.account_id, at=h.clock.now)
        await h.publish_state(last)
        await h.link(mower_id, owner.account_id, connected=False)
        # the gateway's offline state (ADR 0040) may arrive after the event: it is no contact
        await h.publish_state(last.model_copy(update={"connected": False}))
        h.clock.advance(31)
        swept = await h.services.watcher.sweep_offline()
    assert [e.kind.value for e in swept] == ["mower_offline"]


# --- users who never saved preferences -----------------------------------------------------

APNS_TOKEN = "a1" * 32


async def register_iphone(h: Harness, person: Person) -> None:
    """An APNs device of ``person`` (what the iOS app registers), without saving preferences."""
    await h.store.add_apns_device(
        ApnsDevice(uuid4(), person.user_id, person.account_id, APNS_TOKEN, "iPhone", h.clock.now)
    )


async def get_stuck(h: Harness, owner: Person, mower_id: Any) -> None:
    """Mowing, then the mower reports error 1201 ("The robot is stuck")."""
    h.clock.advance(10)
    await h.publish_state(
        make_state(mower_id, owner.account_id, at=h.clock.now, activity=MowerActivity.MOWING)
    )
    h.clock.advance(30)
    await h.publish_state(
        make_state(
            mower_id,
            owner.account_id,
            at=h.clock.now,
            activity=MowerActivity.ERROR,
            error_code="1201",
            error_message="The robot is stuck",
        )
    )


async def test_notifications_flow_user_without_preferences_gets_the_default_kinds() -> None:
    async with harness() as h:
        owner = await h.owner()
        mower_id = await h.add_mower(owner, name="Garten")
        await register_iphone(h, owner)
        assert await h.store.preferences(owner.user_id, owner.account_id) is None
        await get_stuck(h, owner, mower_id)
        h.clock.advance(600)
        await finish_a_mow(h, owner, mower_id)
        items = await h.inbox(owner)
    kinds = [i["kind"] for i in items]
    assert "mower_stuck" in kinds and "mowing_finished" in kinds
    assert "job_started" not in kinds
    stuck = next(i for i in items if i["kind"] == "mower_stuck")
    assert "Garten" in stuck["title"] and "1201" in stuck["body"]
    finished = next(i for i in items if i["kind"] == "mowing_finished")
    assert finished["title"] == "Garten ist fertig mit Mähen"  # the default language is German
    deliveries = [r for r in h.store.data.deliveries.values() if r.user_id == owner.user_id]
    assert deliveries and {r.channel for r in deliveries} == {"log"}  # the configured channel


async def test_notifications_flow_saved_preferences_win_over_the_defaults() -> None:
    async with harness() as h:
        owner = await h.owner()
        mower_id = await h.add_mower(owner)
        await register_iphone(h, owner)
        await h.subscribe_all(owner, enabled_kinds=["battery_low"])
        await get_stuck(h, owner, mower_id)
        h.clock.advance(600)
        await finish_a_mow(h, owner, mower_id)
        assert await h.inbox(owner) == []
        await h.subscribe_all(owner, enabled_kinds=["mowing_finished", "job_started"])
        h.clock.advance(600)
        await finish_a_mow(h, owner, mower_id)
        kinds = [i["kind"] for i in await h.inbox(owner)]
    assert sorted(kinds) == ["job_started", "mowing_finished"]


async def test_notifications_flow_defaults_still_need_can_view_and_a_target() -> None:
    async with harness() as h:
        owner = await h.owner()
        mower_id = await h.add_mower(owner)
        no_grant = await h.member(owner)
        await register_iphone(h, no_grant)
        no_target = await h.member(owner)
        await h.grant_view(no_target, mower_id)
        await finish_a_mow(h, owner, mower_id)
        assert await h.store.inbox(no_grant.user_id, owner.account_id, 10, unread_only=False) == []
        assert await h.store.inbox(no_target.user_id, owner.account_id, 10, unread_only=False) == []
        checks = [c for c in h.authz.checks if str(no_grant.user_id) in str(c)]
    assert checks  # can_view was asked for the default recipient and denied


def raised_events(h: Harness) -> list[Any]:
    from mowdeck_contracts.v1 import NotificationRaised

    return [
        m.model(EventEnvelope[NotificationRaised])
        for m in h.bus.published
        if m.subject == "event.notifications.raised"
    ]


async def test_notifications_flow_raises_every_event_for_the_account() -> None:
    async with harness() as h:
        owner = await h.owner()
        mower_id = await h.add_mower(owner, name="Backyard")
        # The owner never saved preferences; members of the account have no grant: the account
        # copy does not depend on who receives the per-user items.
        await finish_a_mow(h, owner, mower_id)
        envelopes = raised_events(h)
    raised = [e.payload for e in envelopes]
    kinds = [r.kind.value for r in raised]
    # Every derived event, also the dock departure alarm nobody had a command for.
    assert {"job_started", "mower_left_dock_unexpectedly", "mowing_finished"} <= set(kinds)
    assert kinds[-1] == "mowing_finished"
    finished = raised[-1]
    assert finished.account_id == owner.account_id and finished.mower_id == mower_id
    assert finished.title == "Backyard finished mowing"  # English, the account has no language
    assert finished.url == f"/mowers/{mower_id}" and finished.required_relation == "can_view"
    assert envelopes[-1].id == finished.notification_id  # the stable id is the msg id
    assert envelopes[-1].account_id == owner.account_id


async def test_notifications_flow_raised_id_is_stable_per_occurrence() -> None:
    from notifications.application.fanout import raised_id

    account = uuid4()
    assert raised_id(account, "mowing_finished:x:1") == raised_id(account, "mowing_finished:x:1")
    assert raised_id(account, "mowing_finished:x:1") != raised_id(account, "mowing_finished:x:2")
    assert raised_id(account, "k") != raised_id(uuid4(), "k")


async def test_notifications_flow_sent_event_carries_created_at() -> None:
    async with harness() as h:
        owner = await h.owner()
        mower_id = await h.add_mower(owner)
        await h.subscribe_all(owner)
        await finish_a_mow(h, owner, mower_id)
        events = sent_events(h)
    assert events and all(e.created_at is not None for e in events)


async def test_notifications_flow_default_recipient_and_account_copy_together() -> None:
    async with harness() as h:
        owner = await h.owner()
        mower_id = await h.add_mower(owner, name="Garten")
        await register_iphone(h, owner)  # no saved preferences: the defaults apply
        await get_stuck(h, owner, mower_id)
        items = await h.inbox(owner)
        sent = sent_events(h)
        raised = [e.payload for e in raised_events(h)]
    stuck = next(i for i in items if i["kind"] == "mower_stuck")
    [sent_stuck] = [e for e in sent if e.kind.value == "mower_stuck"]
    assert str(sent_stuck.notification_id) == stuck["id"] and sent_stuck.created_at is not None
    [raised_stuck] = [r for r in raised if r.kind.value == "mower_stuck"]
    # The person's copy is German (default language), the account copy English, ids differ.
    assert raised_stuck.notification_id != sent_stuck.notification_id
    assert "Garten" in raised_stuck.title and raised_stuck.title != stuck["title"]
