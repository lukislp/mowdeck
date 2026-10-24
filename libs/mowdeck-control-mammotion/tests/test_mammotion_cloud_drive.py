"""Manual driving over the Mammotion cloud (ADR 0049) on a fake port and a fake remote-drive
session: the capabilities, the session flow, the owner account refusal (50803), no reverse, the
watchdog, blades, vendor ends, the fence warning, the emergency stop, the dual link's choice of
the drive link, and the controller rules on top (reverse refused, a nudge's ttl zeroes)."""

import asyncio
import time
from collections.abc import Callable
from datetime import timedelta
from typing import Any
from uuid import uuid4

import pytest
from mammotion_support import BLE_OPTIONS, CLOUD_OPTIONS, MOWER_ID, ble, clock, cloud, config
from mowdeck_contracts.v1 import (
    CommandEnvelope,
    CommandStatus,
    EmergencyStop,
    ManualDrive,
    MowerActivity,
    PrincipalKind,
    PrincipalRef,
)
from mowdeck_control import (
    CommandRejected,
    FrameDeadlineCapable,
    MowerController,
    NotConnected,
    SensorStop,
    frame_deadline_of,
)
from mowdeck_control.controller import NO_REVERSE_REASON
from mowdeck_control_mammotion import mapping
from mowdeck_control_mammotion.cloud import (
    CLOUD_DRIVE_OFF,
    CLOUD_DRIVE_UNSUPPORTED,
    CLOUD_MIN_DRIVE_POLL_S,
    MammotionCloudConnection,
)
from mowdeck_control_mammotion.cloud_drive import (
    BLADES_NOT_SWITCHED,
    FENCE_WARNING,
    NO_REVERSE,
    OWNER_ACCOUNT_REQUIRED,
    CloudDriveSession,
    end_reason,
)
from mowdeck_control_mammotion.dual import (
    BRIDGE_NOT_CONNECTED,
    MammotionDualConnection,
    link_options_of,
)
from mowdeck_control_mammotion.fake import FakeMammotionPort
from mowdeck_control_mammotion.fake_cloud import (
    FAKE_CLOUD_PASSWORD,
    FAKE_DEVICES,
    fake_cloud_connection_factory,
    fake_pool,
)
from mowdeck_control_mammotion.options import CloudOptions
from mowdeck_control_mammotion.port import (
    LocationData,
    NoTransportAvailableError,
    Priority,
    RemoteDriveInfo,
    WorkMode,
)

ISSUER = PrincipalRef(subject=uuid4(), kind=PrincipalKind.USER)


def _cloud_port() -> FakeMammotionPort:
    port = FakeMammotionPort()
    port.cloud_drive = True
    return port


async def _connected(**options: Any) -> tuple[MammotionCloudConnection, FakeMammotionPort]:
    conn, port = cloud(_cloud_port(), **options)
    await conn.connect()
    return conn, port


async def _wait(predicate: Callable[[], object], *, within: float = 10.0) -> None:
    deadline = time.monotonic() + within
    while not predicate():
        if time.monotonic() > deadline:
            msg = "condition not met in time"
            raise AssertionError(msg)
        await asyncio.sleep(0.005)


def _operations(port: FakeMammotionPort) -> list[tuple[int, str, Priority]]:
    return [
        (c.kwargs["main_ctrl"], c.link, c.priority)
        for c in port.sent
        if c.name == "operate_on_device"
    ]


def _envelope(command: Any) -> CommandEnvelope:
    return CommandEnvelope.create(
        mower_id=MOWER_ID,
        account_id=config().account_id,
        issued_by=ISSUER,
        command=command,
        ttl=timedelta(seconds=30),
    )


# --- capabilities ---------------------------------------------------------------------------


async def test_mammotion_cloud_offers_driving_forwards_only_when_the_mower_supports_it() -> None:
    conn, port = await _connected()
    try:
        caps = await conn.capabilities()
        assert caps.manual_drive is True
        assert caps.drive_reverse is False  # the vendor's cloud drive cannot reverse
        assert caps.manual_drive_blades is False  # allow_manual_blades is off by default
        port.cloud_drive = False  # firmware below 1.30.31.19 / no function 002.002
        assert (await conn.capabilities()).manual_drive is False
    finally:
        await conn.disconnect()


async def test_mammotion_cloud_blades_follow_the_option_like_the_bridge() -> None:
    conn, _ = await _connected(allow_manual_blades=True)
    try:
        caps = await conn.capabilities()
        assert caps.manual_drive and caps.manual_drive_blades
    finally:
        await conn.disconnect()


async def test_mammotion_cloud_drive_can_be_switched_off_and_needs_a_map() -> None:
    conn, _ = await _connected(cloud_drive=False)
    try:
        assert (await conn.capabilities()).manual_drive is False
        with pytest.raises(CommandRejected, match=CLOUD_DRIVE_OFF):
            await conn.start_drive_session()
    finally:
        await conn.disconnect()
    conn, port = await _connected()
    try:
        port.device().report_data.locations = [LocationData(bol_hash=0)]
        port.device().map.area.clear()
        assert not mapping.has_vendor_map(port.device())
        assert (await conn.capabilities()).manual_drive is False
        with pytest.raises(CommandRejected, match=r"firmware 1.30.31.19"):
            await conn.start_drive_session()
        assert CLOUD_DRIVE_UNSUPPORTED.startswith("this mower cannot be driven")
    finally:
        await conn.disconnect()


# --- the session ----------------------------------------------------------------------------


async def test_mammotion_cloud_session_takes_the_token_and_drives_with_the_bluetooth_units() -> (
    None
):
    conn, port = await _connected()
    try:
        port.sent.clear()
        session = await conn.start_drive_session()
        remote = port.remote
        assert isinstance(session, CloudDriveSession) and session.is_open
        assert (remote.starts, remote.confirms, remote.phase) == (1, 1, "active")
        # The opening operate_on_device as over Bluetooth: blades off with max_run_speed.
        opening = [c for c in port.sent if c.name == "operate_on_device"]
        assert [(c.kwargs["main_ctrl"], c.link) for c in opening] == [(0, "cloud")]
        assert 0 < opening[0].kwargs["max_run_speed"] <= 1.0
        assert conn._mode() == "drive"  # reports about once a second while driving
        await session.update(0.3, 0.0, False)
        await _wait(lambda: remote.frames)
        assert remote.frames == [mapping.movement_wire(0.3, 0.0)] == [(150, 0)]
        await session.update(0.0, 0.4, False)
        await _wait(lambda: len(remote.frames) == 2)
        assert remote.frames[-1] == mapping.movement_wire(0.0, 0.4)
        assert not any(c.name == "send_movement" for c in port.sent)  # no Bluetooth frame
        await session.close()
        assert remote.frames[-1] == (0, 0) and remote.phase == "idle" and remote.releases == 1
        assert _operations(port)[-1] == (0, "cloud", Priority.EMERGENCY)
        assert not session.is_open and conn._mode() != "drive"
        await session.close()  # idempotent
        assert remote.releases == 1
    finally:
        await conn.disconnect()


async def test_mammotion_cloud_shared_account_gets_the_owner_account_reason() -> None:
    conn, port = await _connected()
    try:
        port.remote.refuse = "50803"
        with pytest.raises(CommandRejected) as refused:
            await conn.start_drive_session()
        assert refused.value.reason == OWNER_ACCOUNT_REQUIRED
        assert "owner account" in refused.value.reason and "50803" in refused.value.reason
        assert port.remote.listener_count == 0  # nothing left subscribed
        assert not conn._drive_active()
        port.remote.refuse = "60001"
        with pytest.raises(CommandRejected, match=r"gave no drive token \(code 60001\)"):
            await conn.start_drive_session()
        port.remote.refuse = None
        port.remote.occupied = True
        with pytest.raises(CommandRejected, match="someone else is driving"):
            await conn.start_drive_session()
    finally:
        await conn.disconnect()


async def test_mammotion_cloud_without_a_cloud_transport_is_not_connected() -> None:
    conn, port = await _connected()
    try:
        port.remote.start_error = NoTransportAvailableError("offline")
        with pytest.raises(NotConnected, match="no usable Mammotion cloud connection"):
            await conn.start_drive_session()
    finally:
        await conn.disconnect()


async def test_mammotion_cloud_refuses_reverse_instead_of_cutting_it_off() -> None:
    conn, port = await _connected()
    try:
        session = await conn.start_drive_session()
        with pytest.raises(CommandRejected, match=NO_REVERSE):
            await session.update(-0.4, 0.0, False)
        await asyncio.sleep(0.05)
        assert port.remote.frames == []  # nothing was sent, not even a zero-cut frame
        await session.close()
    finally:
        await conn.disconnect()


async def test_mammotion_cloud_watchdog_zeroes_when_frames_stop() -> None:
    conn, port = await _connected()
    try:
        session = await conn.start_drive_session()
        assert isinstance(session, FrameDeadlineCapable)
        assert isinstance(session, CloudDriveSession)
        await session.update(0.5, 0.0, False)
        session.expect_next_frame_within(0.1)  # a nudge's ttl, shortened
        await _wait(lambda: port.remote.frames)
        assert port.remote.moving
        await _wait(lambda: not port.remote.moving, within=2.0)
        assert port.remote.frames[-1] == (0, 0)
        assert session.watchdog_stops == 1 and session.is_open  # the session stays open
        # A fresh frame moves again; frames within their ttl are not zeroed.
        await session.update(0.5, 0.0, False)
        session.expect_next_frame_within(1.0)
        await _wait(lambda: port.remote.moving)
        await asyncio.sleep(0.2)
        assert port.remote.moving and session.watchdog_stops == 1
        await session.close()
    finally:
        await conn.disconnect()


async def test_mammotion_cloud_blades_switch_like_over_bluetooth() -> None:
    conn, port = await _connected(allow_manual_blades=True, manual_blade_height_mm=45)
    try:
        session = await conn.start_drive_session()
        port.sent.clear()
        await session.update(0.3, 0.0, True)
        assert _operations(port) == [(1, "cloud", Priority.USER)]
        on = next(c for c in port.sent if c.name == "operate_on_device")
        assert on.kwargs["cut_knife_height"] == 45 and on.kwargs["cut_knife_ctrl"] == 1
        await session.update(0.3, 0.0, True)
        assert len(_operations(port)) == 1  # no repeat while unchanged
        await session.update(0.3, 0.0, False)
        assert _operations(port)[-1] == (0, "cloud", Priority.USER)
        await session.update(0.3, 0.0, True)
        port.sent.clear()
        await session.stop()  # dead-man: wheels and blades off, session open
        assert _operations(port) == [(0, "cloud", Priority.EMERGENCY)]
        assert port.remote.frames[-1] == (0, 0) and session.is_open
        await session.close()
    finally:
        await conn.disconnect()


async def test_mammotion_cloud_blades_stay_off_when_not_allowed_or_not_switched() -> None:
    conn, port = await _connected()
    try:
        session = await conn.start_drive_session()
        port.sent.clear()
        await session.update(0.3, 0.0, True)  # not allowed: kept off, no command
        assert _operations(port) == []
        await session.close()
    finally:
        await conn.disconnect()
    conn, port = await _connected(allow_manual_blades=True)
    try:
        session = await conn.start_drive_session()
        assert isinstance(session, CloudDriveSession)
        port.fail["operate_on_device"] = RuntimeError("refused by the device")
        with pytest.raises(CommandRejected, match=BLADES_NOT_SWITCHED):
            await session.update(0.3, 0.0, True)
        assert not session.blades_on
        await session.close()  # the blades-off attempt fails quietly
        assert port.remote.releases == 1
    finally:
        await conn.disconnect()


async def test_mammotion_cloud_sensor_stop_ends_but_a_stuck_mower_may_be_driven() -> None:
    conn, port = await _connected()
    try:
        session = await conn.start_drive_session()
        device = port.device()
        device.report_data.dev.sys_status = int(WorkMode.MODE_SYSTEM_ERROR)  # not recoverable
        await port.emit()
        assert (await conn.get_state()).activity is MowerActivity.ERROR
        with pytest.raises(SensorStop):
            await session.update(0.3, 0.0, False)
        await session.close()
    finally:
        await conn.disconnect()


@pytest.mark.parametrize("link", ["cloud", "ble"])
async def test_mammotion_a_drive_recoverable_error_admits_frames(link: str) -> None:
    """Controller rule 8 (Vision recovery): a stuck mower may be driven free over either link;
    a lift or tilt may not."""
    port = _cloud_port()
    conn: Any = cloud(port)[0] if link == "cloud" else ble(port)[0]
    await conn.connect()
    try:
        session = await conn.start_drive_session()
        stuck = (await conn.get_state()).model_copy(
            update={
                "activity": MowerActivity.ERROR,
                "error_code": "1201",
                "drive_recoverable": True,
            }
        )
        conn._snapshot = lambda: stuck
        assert conn._sensor_stop_reason() is None
        await session.update(0.3, 0.0, False)
        lifted = stuck.model_copy(update={"error_code": "2801", "drive_recoverable": False})
        conn._snapshot = lambda: lifted
        with pytest.raises(SensorStop, match="2801"):
            await session.update(0.3, 0.0, False)
        await session.close()
    finally:
        await conn.disconnect()


@pytest.mark.parametrize(
    ("kind", "error", "words"),
    [
        ("network_poor", NotConnected, "too slow"),
        ("token_expired", NotConnected, "token expired"),
        ("ble_preempt", NotConnected, "over Bluetooth"),
        ("idle_timeout", CommandRejected, "without input"),
        ("out_of_fence", CommandRejected, "left its mapped area"),
    ],
)
async def test_mammotion_cloud_vendor_ends_surface_on_the_next_frame(
    kind: str, error: type[Exception], words: str
) -> None:
    conn, port = await _connected()
    try:
        session = await conn.start_drive_session()
        await session.update(0.3, 0.0, False)
        await _wait(lambda: port.remote.frames)
        port.sent.clear()
        await port.remote.end_by_vendor(kind)
        assert session.is_open and session.ended is not None and not conn._drive_active()
        assert _operations(port) == [(0, "cloud", Priority.EMERGENCY)]  # blades off as well
        with pytest.raises(error, match=words):
            await session.update(0.3, 0.0, False)
        await session.close()
        assert port.remote.releases == 1  # the vendor released it already
    finally:
        await conn.disconnect()


async def test_mammotion_cloud_fence_warning_ends_and_a_new_session_acknowledges_it() -> None:
    conn, port = await _connected()
    try:
        session = await conn.start_drive_session()
        await session.update(0.5, 0.0, False)
        await _wait(lambda: port.remote.frames)
        await port.remote.approach_fence()
        assert session.ended is not None and port.remote.phase == "idle"  # zeroed, released
        with pytest.raises(CommandRejected, match="beyond its mapped area"):
            await session.update(0.5, 0.0, False)
        await session.close()
        # Starting again is the acknowledgement: the next warning resumes input at once.
        again = await conn.start_drive_session()
        await again.update(0.4, 0.0, False)
        await port.remote.approach_fence()
        assert port.remote.acknowledged == 1 and not port.remote.fence_paused
        assert again.is_open
        await again.close()
    finally:
        await conn.disconnect()


async def test_mammotion_cloud_emergency_stop_zeroes_and_releases_first() -> None:
    conn, port = await _connected()
    try:
        session = await conn.start_drive_session()
        await session.update(0.6, 0.0, False)
        await _wait(lambda: port.remote.moving)
        port.sent.clear()
        outcome = await conn.execute(EmergencyStop())
        assert outcome.status is CommandStatus.ACCEPTED
        assert not port.remote.moving and port.remote.releases == 1
        assert not session.is_open and not conn._drive_active()
        names = [c.name for c in port.sent]
        assert names[-3:] == ["pause_execute_task", "send_movement", "operate_on_device"]
    finally:
        await conn.disconnect()


async def test_mammotion_cloud_disconnect_zeroes_and_releases_the_session() -> None:
    conn, port = await _connected()
    session = await conn.start_drive_session()
    await session.update(0.6, 0.0, False)
    await _wait(lambda: port.remote.moving)
    await conn.disconnect()
    assert not port.remote.moving and port.remote.releases == 1 and not session.is_open


async def test_mammotion_cloud_calibration_cannot_poll_faster_than_once_a_second() -> None:
    conn, _ = await _connected()
    try:
        conn.set_drive_report_interval(0.5)
        assert conn.drive_poll_s == CLOUD_MIN_DRIVE_POLL_S == 1.0
        conn.set_drive_report_interval(None)
        assert conn.drive_poll_s == 1.0
    finally:
        await conn.disconnect()


def test_mammotion_cloud_end_reasons_name_no_account() -> None:
    assert end_reason(RemoteDriveInfo("token_unavailable", "50803")) == OWNER_ACCOUNT_REQUIRED
    assert end_reason(RemoteDriveInfo("approach_fence")) == FENCE_WARNING
    assert "13*****789" not in end_reason(RemoteDriveInfo("occupied_by_other", "13*****789"))
    assert "something_new" in end_reason(RemoteDriveInfo("something_new"))
    assert "refused" in end_reason(None)


# --- the controller on top --------------------------------------------------------------------


async def test_mammotion_cloud_controller_rejects_reverse_and_zeroes_a_nudge() -> None:
    conn, port = await _connected()
    controller = MowerController(conn, mower_id=MOWER_ID)
    await controller.start()
    try:
        backwards = await controller.handle(
            _envelope(ManualDrive(linear=-0.4, angular=0.0, ttl_ms=500))
        )
        assert backwards.status is CommandStatus.REJECTED
        assert backwards.reason == NO_REVERSE_REASON
        assert port.remote.starts == 0  # refused before a session opened
        nudge = await controller.handle(_envelope(ManualDrive(linear=0.4, angular=0, ttl_ms=200)))
        assert nudge.status is CommandStatus.ACCEPTED
        assert frame_deadline_of(controller._session) is not None
        await _wait(lambda: port.remote.moving)
        # The dead-man ends the session after the ttl: zero frame, token released.
        await _wait(lambda: port.remote.releases == 1, within=3.0)
        assert port.remote.frames[-1] == (0, 0) and not port.remote.moving
    finally:
        await controller.close()
        await conn.disconnect()


# --- the dual link --------------------------------------------------------------------------


class _NoBridge:
    """A proxy opener that never reaches the bridge."""

    async def __call__(self, **kwargs: Any) -> Any:
        raise ConnectionError("no route")


async def _no_sleep(_seconds: float) -> None:
    await asyncio.sleep(0.01)


async def test_mammotion_dual_drives_over_the_cloud_while_the_bridge_is_down() -> None:
    port = _cloud_port()
    conn = MammotionDualConnection(
        port,
        config(),
        CloudOptions.parse(CLOUD_OPTIONS),
        link_options_of(BLE_OPTIONS),
        clock=clock,
        opener=_NoBridge(),
        sleep=_no_sleep,
        poll_s=0.01,
    )
    await conn.connect()
    try:
        caps = await conn.capabilities()
        assert caps.manual_drive is True and caps.drive_reverse is False
        session = await conn.start_drive_session()
        assert isinstance(session, CloudDriveSession)
        assert conn.bridge_roles() == frozenset()  # Bluetooth is not wanted for it
        await session.update(0.3, 0.0, False)
        await _wait(lambda: port.remote.frames)
        await session.close()
        port.cloud_drive = False
        with pytest.raises(NotConnected, match=BRIDGE_NOT_CONNECTED):
            await conn.start_drive_session()
    finally:
        await conn.disconnect()


# --- the fake cloud -------------------------------------------------------------------------


async def test_mammotion_fake_cloud_refuses_cloud_driving_for_a_shared_account() -> None:
    factory = fake_cloud_connection_factory(fake_pool(linger_seconds=0))
    name = FAKE_DEVICES[0].device_name
    owner = factory(
        config(
            {
                "account": "owner@fake.mowdeck.test",
                "password": FAKE_CLOUD_PASSWORD,
                "device_name": name,
            }
        )
    )
    shared = factory(
        config(
            {
                "account": "shared-anna@fake.mowdeck.test",
                "password": FAKE_CLOUD_PASSWORD,
                "device_name": name,
            }
        )
    )
    await owner.connect()
    await shared.connect()
    try:
        assert (await owner.capabilities()).manual_drive is True
        session = await owner.start_drive_session()
        await session.update(0.3, 0.0, False)
        await session.close()
        with pytest.raises(CommandRejected, match="owner account"):
            await shared.start_drive_session()
    finally:
        await owner.disconnect()
        await shared.disconnect()
