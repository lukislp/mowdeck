"""Theft alarm and blade reminder end to end: bus messages in, inbox, deliveries and the ack API."""

from datetime import timedelta
from typing import Any
from uuid import UUID, uuid4

from mowdeck_contracts.v1 import (
    TELEMETRY_BLADE_HOURS_THRESHOLD,
    BladeHoursThreshold,
    CommandEnvelope,
    CommandPriority,
    EventEnvelope,
    GeoReference,
    ManualDrive,
    MowerActivity,
    MowerErrorCategory,
    MowerMapSnapshot,
    PrincipalKind,
    PrincipalRef,
    RtkFixStatus,
    StartMow,
    drive_correlation_id,
)
from mowdeck_contracts.v1.subjects import command_subject, device_subject
from mowdeck_control import default_map
from mowdeck_control.geo import to_wgs84
from mowdeck_service_kit.bus import InMemoryBus, publish_event
from notifications_support import API, T0, Harness, Person, harness, make_state

ALL_KINDS = [
    "mowing_finished",
    "mower_stuck",
    "battery_low",
    "mower_offline",
    "mower_online",
    "mower_lifted",
    "mower_left_dock_unexpectedly",
    "geofence_exit",
    "blade_maintenance",
]
SIM_MAP = default_map()


def command(
    mower_id: UUID,
    account_id: UUID,
    h: Harness,
    *,
    drive: bool = False,
    issued_offset: float = 0,
) -> CommandEnvelope:
    issued = h.clock.now + timedelta(seconds=issued_offset)
    envelope = CommandEnvelope(
        mower_id=mower_id,
        account_id=account_id,
        issued_by=PrincipalRef(subject=uuid4(), kind=PrincipalKind.SERVICE),
        issued_at=issued,
        expires_at=issued + timedelta(seconds=60),
        priority=CommandPriority.NORMAL,
        command=ManualDrive(linear=0.5, angular=0) if drive else StartMow(area_ids=["front"]),
    )
    if drive:
        envelope = envelope.model_copy(update={"correlation_id": drive_correlation_id(uuid4())})
    return envelope


def snapshot(
    mower_id: UUID, account_id: UUID, *, seconds_after: float = 0, **changes: Any
) -> MowerMapSnapshot:
    return MowerMapSnapshot(
        mower_id=mower_id,
        account_id=account_id,
        observed_at=T0 + timedelta(seconds=seconds_after),
        map=SIM_MAP.model_copy(update=changes) if changes else SIM_MAP,
    )


async def state(
    h: Harness, owner: Person, mower_id: UUID, activity: MowerActivity, after: float = 10, **kw: Any
) -> None:
    h.clock.advance(after)
    await h.publish_state(
        make_state(mower_id, owner.account_id, at=h.clock.now, activity=activity, **kw)
    )


async def lift(h: Harness, owner: Person, mower_id: UUID, after: float) -> None:
    """Clear any error, wait ``after`` seconds, then lift the mower."""
    await state(h, owner, mower_id, MowerActivity.PAUSED, 1, position=(48.1, 11.5))
    await state(
        h,
        owner,
        mower_id,
        MowerActivity.ERROR,
        after,
        error_code="lifted",
        error_category=MowerErrorCategory.LIFT_TILT,
        position=(48.1, 11.5),
    )


async def test_notifications_theft_alarm_bypasses_quiet_hours() -> None:
    async with harness() as h:
        owner = await h.owner()
        mower_id = await h.add_mower(owner, name="Backyard")
        # T0 is 12:00 UTC = 14:00 in Berlin: quiet
        await h.subscribe_all(
            owner,
            enabled_kinds=ALL_KINDS,
            timezone="Europe/Berlin",
            quiet_hours={"start": "13:30", "end": "15:00"},
            language="de",
        )
        await state(h, owner, mower_id, MowerActivity.MOWING, battery=50)
        await state(
            h,
            owner,
            mower_id,
            MowerActivity.ERROR,
            battery=10,
            # as the Mammotion adapter reports a lift sensor stop: the code with its category
            error_code="2801",
            error_category=MowerErrorCategory.LIFT_TILT,
            position=(48.1234567, 11.5),
        )
        items = await h.inbox(owner)
        await h.services.outbox.run_once()
    assert sorted(i["kind"] for i in items) == ["battery_low", "mower_lifted"]
    lifted = next(i for i in items if i["kind"] == "mower_lifted")
    assert lifted["severity"] == "critical"
    assert lifted["title"] == "Alarm: Backyard wurde angehoben"
    assert "48.123457, 11.500000" in lifted["body"]
    assert lifted["url"] == f"/mowers/{mower_id}/map?lat=48.123457&lon=11.500000"
    assert lifted["acked"] is False and lifted["acked_at"] is None
    assert [d.message.kind.value for d in h.channel.sent] == ["mower_lifted"]


async def test_notifications_theft_ack_silences_repeats_for_an_hour() -> None:
    async with harness() as h:
        owner = await h.owner()
        mower_id = await h.add_mower(owner)
        member = await h.member(owner)
        await h.grant_view(member, mower_id)
        await h.subscribe_all(owner, enabled_kinds=ALL_KINDS)
        await h.subscribe_all(member, enabled_kinds=ALL_KINDS)
        await state(h, owner, mower_id, MowerActivity.MOWING, position=(48.1, 11.5))
        await lift(h, owner, mower_id, 10)
        (item,) = await h.inbox(owner)
        foreign = await h.client.post(f"{API}/inbox/{item['id']}/ack", headers=member.headers)
        assert foreign.status_code == 404
        acked = await h.client.post(f"{API}/inbox/{item['id']}/ack", headers=owner.headers)
        assert acked.status_code == 200, acked.text
        body = acked.json()
        assert body["acked"] is True and body["read"] is True
        again = await h.client.post(f"{API}/inbox/{item['id']}/ack", headers=owner.headers)
        assert again.json()["acked_at"] == body["acked_at"]  # idempotent
        await lift(h, owner, mower_id, 700)  # after the alarm cooldown, within the silence
        assert len(await h.inbox(owner)) == 1
        assert len(await h.inbox(member)) == 2  # the member did not acknowledge
        await lift(h, owner, mower_id, 3000)  # the silence (3600 s) is over
        owner_items = await h.inbox(owner)
        missing = await h.client.post(f"{API}/inbox/{uuid4()}/ack", headers=owner.headers)
    assert [i["kind"] for i in owner_items] == ["mower_lifted", "mower_lifted"]
    assert owner_items[0]["acked"] is False
    assert missing.status_code == 404


async def test_notifications_theft_ack_of_other_kinds_silences_nothing() -> None:
    async with harness() as h:
        owner = await h.owner()
        mower_id = await h.add_mower(owner)
        await h.subscribe_all(owner, enabled_kinds=ALL_KINDS)
        await state(h, owner, mower_id, MowerActivity.MOWING, battery=50)
        await state(h, owner, mower_id, MowerActivity.MOWING, battery=10)
        (item,) = await h.inbox(owner)
        acked = await h.client.post(f"{API}/inbox/{item['id']}/ack", headers=owner.headers)
    assert acked.json()["acked"] is True
    assert h.store.data.silences == {}


async def test_notifications_theft_left_dock_and_command_consumer() -> None:
    async with harness(notifications_dock_confirm_seconds=20) as h:
        owner = await h.owner()
        mower_id = await h.add_mower(owner)
        await h.subscribe_all(owner, enabled_kinds=["mower_left_dock_unexpectedly"])
        # a command before any state is ignored (no watch yet)
        await h.bus.publish(command_subject(mower_id), command(mower_id, owner.account_id, h))
        assert mower_id not in h.store.data.watches
        await state(h, owner, mower_id, MowerActivity.DOCKED)
        # the scheduler starts the mower: no alarm (command consumed before the state)
        await h.bus.publish(command_subject(mower_id), command(mower_id, owner.account_id, h))
        await state(h, owner, mower_id, MowerActivity.MOWING, 2)
        await state(h, owner, mower_id, MowerActivity.MOWING, 30)
        await state(h, owner, mower_id, MowerActivity.DOCKED, 600)
        assert await h.inbox(owner) == []
        # nobody sent a command: held, then alarmed by the next state
        await state(h, owner, mower_id, MowerActivity.MOWING, 3600, position=(48.1, 11.5))
        assert await h.inbox(owner) == []
        await state(h, owner, mower_id, MowerActivity.MOWING, 30, position=(48.1, 11.5))
        items = await h.inbox(owner)
    assert [i["kind"] for i in items] == ["mower_left_dock_unexpectedly"]
    assert items[0]["title"] == "Alarm: Front lawn left the dock"
    assert items[0]["url"].startswith(f"/mowers/{mower_id}/map?lat=48.100000")


async def test_notifications_theft_drive_frames_are_throttled_and_redelivery_is_harmless() -> None:
    async with harness() as h:
        owner = await h.owner()
        mower_id = await h.add_mower(owner)
        await state(h, owner, mower_id, MowerActivity.DOCKED)
        watcher = h.services.watcher
        frame = command(mower_id, owner.account_id, h, drive=True)
        assert await watcher.on_command(frame) is True
        watch = h.store.data.watches[mower_id]
        assert watch.last_drive_at == frame.issued_at == watch.last_command_at
        assert await watcher.on_command(frame) is False  # redelivered
        soon = command(mower_id, owner.account_id, h, drive=True, issued_offset=1)
        assert await watcher.on_command(soon) is False  # throttled
        later = command(mower_id, owner.account_id, h, drive=True, issued_offset=6)
        assert await watcher.on_command(later) is True
        start = command(mower_id, owner.account_id, h, issued_offset=-30)
        assert await watcher.on_command(start) is False  # older than the drive frame
        assert h.store.data.watches[mower_id].last_command_at == later.issued_at


async def test_notifications_theft_geofence_from_the_map_snapshot() -> None:
    bus = InMemoryBus()
    mower_id, account_id = uuid4(), uuid4()
    # the gateway published the map before this service started (last-per-subject consumer)
    await bus.publish(device_subject(mower_id, "map"), snapshot(mower_id, account_id))
    async with harness(bus=bus, notifications_app_url="https://app.example.test/") as h:
        owner = await h.owner(account_id=account_id)
        await h.add_mower(owner, mower_id=mower_id)
        await h.subscribe_all(owner, enabled_kinds=ALL_KINDS)
        stored = await h.store.geofence(mower_id)
        assert stored is not None and len(stored.fence.areas) == 2
        inside = to_wgs84(SIM_MAP, 5, 4)
        far = to_wgs84(SIM_MAP, 32, 4)
        await state(h, owner, mower_id, MowerActivity.MOWING, position=inside)
        await state(h, owner, mower_id, MowerActivity.MOWING, position=far)
        await state(
            h, owner, mower_id, MowerActivity.MOWING, position=far, rtk_fix=RtkFixStatus.FIXED
        )
        items = await h.inbox(owner)
    assert [i["kind"] for i in items] == ["geofence_exit"]
    assert items[0]["body"].startswith("The mower is 15 m outside its mapped areas")
    assert items[0]["url"] == (
        f"https://app.example.test/mowers/{mower_id}/map?lat={far[0]:.6f}&lon={far[1]:.6f}"
    )


async def test_notifications_theft_snapshot_handling() -> None:
    bus = InMemoryBus()
    mower_id = uuid4()
    async with harness(bus=bus) as h:
        owner = await h.owner()
        await h.announce(mower_id, owner.account_id)
        await h.bus.publish(
            device_subject(mower_id, "map"), snapshot(mower_id, owner.account_id, seconds_after=60)
        )
        watcher = h.services.watcher
        assert await watcher.on_map(snapshot(mower_id, owner.account_id)) is False  # older
        assert await watcher.on_map(snapshot(mower_id, owner.account_id, areas=[])) is False
        placeholder = GeoReference(origin_lat=0, origin_lon=0)
        assert (
            await watcher.on_map(snapshot(mower_id, owner.account_id, reference=placeholder))
            is False
        )
        assert await watcher.on_map(snapshot(mower_id, owner.account_id, seconds_after=90)) is True
        await h.bus.publish(device_subject(mower_id, "capabilities"), b"{}")  # ignored
        dropped = len(h.bus.dropped)
        await h.bus.publish(device_subject(mower_id, "map"), b"{}")
        await h.bus.publish(device_subject(uuid4(), "map"), snapshot(mower_id, owner.account_id))
        await h.bus.publish("device.mower.not-a-uuid.map", snapshot(mower_id, owner.account_id))
        assert len(h.bus.dropped) == dropped + 3
        await h.announce(mower_id, owner.account_id, removed=True)
        assert await h.store.geofence(mower_id) is None


async def test_notifications_theft_cross_account_messages_are_dropped() -> None:
    async with harness() as h:
        owner = await h.owner()
        mower_id = await h.add_mower(owner)
        await h.subscribe_all(owner, enabled_kinds=ALL_KINDS)
        await state(h, owner, mower_id, MowerActivity.DOCKED)
        stranger = uuid4()
        await h.bus.publish(command_subject(mower_id), command(mower_id, stranger, h))
        await h.bus.publish(device_subject(mower_id, "map"), snapshot(mower_id, stranger))
        await publish_event(
            h.bus,
            EventEnvelope[BladeHoursThreshold](
                type=TELEMETRY_BLADE_HOURS_THRESHOLD,
                producer="telemetry",
                account_id=stranger,
                payload=BladeHoursThreshold(
                    mower_id=mower_id,
                    account_id=stranger,
                    blade_hours_since_change=60,
                    threshold_hours=50,
                    crossed_at=h.clock.now,
                ),
            ),
        )
        await h.publish_state(make_state(mower_id, stranger, at=h.clock.now))
        # malformed commands and a subject naming another mower
        await h.bus.publish(command_subject(mower_id), b"{}")
        await h.bus.publish(command_subject(uuid4()), command(mower_id, owner.account_id, h))
        await h.bus.publish("command.mower.nope", command(mower_id, owner.account_id, h))
        assert len(h.bus.dropped) == 7
        assert h.store.data.watches[mower_id].last_command_at is None
        assert await h.store.geofence(mower_id) is None
        assert await h.inbox(owner) == []


async def test_notifications_blade_maintenance_reaches_the_inbox() -> None:
    async with harness() as h:
        owner = await h.owner()
        mower_id = await h.add_mower(owner, name="Vorgarten")
        await h.subscribe_all(owner, enabled_kinds=ALL_KINDS, language="de")
        await state(h, owner, mower_id, MowerActivity.DOCKED)
        crossing = BladeHoursThreshold(
            mower_id=mower_id,
            account_id=owner.account_id,
            blade_hours_since_change=50.4,
            threshold_hours=50,
            crossed_at=h.clock.now,
        )

        async def publish(event_type: str, payload: Any) -> None:
            await publish_event(
                h.bus,
                EventEnvelope[dict[str, Any]](
                    type=event_type,
                    producer="telemetry",
                    account_id=owner.account_id,
                    payload=payload,
                ),
            )

        await publish(TELEMETRY_BLADE_HOURS_THRESHOLD, crossing.model_dump(mode="json"))
        await publish(TELEMETRY_BLADE_HOURS_THRESHOLD, crossing.model_dump(mode="json"))  # again
        await publish("telemetry.something.else", {"x": 1})  # ignored
        dropped = len(h.bus.dropped)
        await publish(TELEMETRY_BLADE_HOURS_THRESHOLD, {"x": 1})
        await h.bus.publish("event.telemetry.blade_hours.threshold", b"nope")
        assert len(h.bus.dropped) == dropped + 2
        items = await h.inbox(owner)
        await h.services.outbox.run_once()
    assert [i["kind"] for i in items] == ["blade_maintenance"]
    assert items[0]["severity"] == "info"
    assert items[0]["title"] == "Vorgarten: Zeit für neue Messer"
    assert (
        items[0]["body"]
        == "Die Messer liefen seit dem letzten Wechsel 50 Std. (Erinnerung ab 50 Std.)."
    )
    assert items[0]["url"] == f"/mowers/{mower_id}"
    assert [d.message.kind.value for d in h.channel.sent] == ["blade_maintenance"]


async def test_notifications_theft_kinds_are_default_on_for_new_users() -> None:
    async with harness() as h:
        owner = await h.owner()
        await h.add_mower(owner)
        response = await h.client.get(f"{API}/preferences", headers=owner.headers)
    body = response.json()
    assert body["stored"] is False
    for kind in (
        "mower_lifted",
        "mower_left_dock_unexpectedly",
        "geofence_exit",
        "blade_maintenance",
    ):
        assert kind in body["enabled_kinds"] and kind in body["available_kinds"]
