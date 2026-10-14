"""The cloud connection and the shared base on a fake PyMammotion port."""

import asyncio
from datetime import timedelta
from uuid import uuid4

import pytest
from mammotion_support import ACCOUNT_ID, CLOUD_OPTIONS, MOWER_ID, NOW, ble, cloud, config
from mowdeck_contracts.v1 import (
    CommandEnvelope,
    CommandStatus,
    ConnectionKind,
    EmergencyStop,
    MowerActivity,
    PauseMow,
    PositionSource,
    PrincipalKind,
    PrincipalRef,
    ResumeMow,
    ReturnToDock,
    SetBladeHeight,
    SetSpeed,
    SpotMowPlan,
    SpotNativeJob,
    SpotSegment,
    StartMow,
)
from mowdeck_control import (
    CommandRejected,
    MapRebuildable,
    MowerController,
    NotConnected,
    SensorStop,
)
from mowdeck_control_mammotion import base as mammotion_base
from mowdeck_control_mammotion.base import LIGHT_READ_COMMAND, UNCHANGED_REPUBLISH_S, MammotionBase
from mowdeck_control_mammotion.cloud import MammotionCloudConnection
from mowdeck_control_mammotion.fake import (
    BACK_HASH,
    FRONT_HASH,
    RTK_YAW_DEG,
    FakeMammotionPort,
    dock_buffer,
    sample_device,
)
from mowdeck_control_mammotion.options import CloudOptions
from mowdeck_control_mammotion.port import (
    CommandTimeoutError,
    NoTransportAvailableError,
    Priority,
    TransportError,
    WorkMode,
)

DOCK_ARGS = {"rw_id": 5, "context": 1, "rw": 1}


def _is_info_request(name: str) -> bool:
    """The connect-time device information, dock buffer and light read requests."""
    return name.startswith("get_device_") or name in {"read_write_device", LIGHT_READ_COMMAND}


FRONT = str(FRONT_HASH)


async def test_mammotion_cloud_connect_state_and_disconnect() -> None:
    conn, port = cloud()
    assert conn.kind is ConnectionKind.CLOUD
    assert not conn.is_connected
    with pytest.raises(NotConnected):
        await conn.get_state()
    await conn.connect()
    await conn.connect()  # idempotent
    assert port.opened == 1 and conn.is_connected
    state = await conn.get_state()
    assert state.mower_id == MOWER_ID and state.account_id == ACCOUNT_ID
    assert state.connection is ConnectionKind.CLOUD
    assert state.raw["link"] == "cloud"
    port.link = False
    assert (await conn.get_state()).raw["link"] == "cloud (no usable transport)"
    assert conn.is_connected  # a transient MQTT loss does not end the connection
    port.reauth_required = True
    assert not conn.is_connected  # a dead login does
    port.reauth_required = False
    await conn.disconnect()
    await conn.disconnect()
    assert port.closed == 1 and port.listener_count == 0
    assert not conn.is_connected


async def test_mammotion_connect_failure_closes_the_port() -> None:
    port = FakeMammotionPort()
    port.open_error = RuntimeError("login failed")
    conn, _ = cloud(port)
    with pytest.raises(RuntimeError):
        await conn.connect()
    assert port.closed == 1 and not conn.is_connected


async def test_mammotion_refuses_a_device_that_is_not_a_mower() -> None:
    conn, port = cloud(FakeMammotionPort(device_name="RTK12345"))
    with pytest.raises(CommandRejected, match="not a mower"):
        await conn.connect()
    assert port.closed == 1


async def test_mammotion_watch_state_pushes_changes_only() -> None:
    conn, port = cloud()
    await conn.connect()
    watch = conn.watch_state()
    first = await anext(watch)
    assert first.activity is MowerActivity.DOCKED
    await port.emit()  # nothing changed: not published again
    port.set_mode(WorkMode.MODE_WORKING)
    port.device().report_data.dev.charge_state = 0
    await port.emit()
    second = await asyncio.wait_for(anext(watch), 1)
    assert second.activity is MowerActivity.MOWING
    await conn.disconnect()
    with pytest.raises(StopAsyncIteration):
        await asyncio.wait_for(anext(watch), 1)


async def test_mammotion_unchanged_state_is_republished_after_a_while() -> None:
    port = FakeMammotionPort()
    ticks = [100.0]
    times = iter(NOW + timedelta(seconds=s) for s in range(100))
    conn = MammotionCloudConnection(
        port,
        config(),
        CloudOptions.parse(CLOUD_OPTIONS),
        clock=lambda: next(times),
    )
    conn._publish_clock = lambda: ticks[0]
    await conn.connect()
    watch = conn.watch_state()
    first = await anext(watch)  # the latest snapshot
    await port.emit()  # an identical report right after connect: not published again
    ticks[0] += UNCHANGED_REPUBLISH_S
    await port.emit()  # an identical report later: published, observed_at is honest
    again = await asyncio.wait_for(anext(watch), 1)
    assert again.activity is first.activity
    assert again.observed_at > first.observed_at
    await conn.disconnect()


async def test_mammotion_refresh_loop_asks_for_fresh_state_until_disconnect() -> None:
    port = FakeMammotionPort()
    conn = MammotionCloudConnection(
        port, config(), CloudOptions.parse(CLOUD_OPTIONS), clock=lambda: NOW
    )
    conn._refresh_interval_s = 0.01  # the option allows 10 s at least; shortened for the test
    await conn.connect()
    for _ in range(200):
        if len(port.fresh_requests) >= 3:
            break
        await asyncio.sleep(0.01)
    assert len(port.fresh_requests) >= 3
    assert set(port.fresh_requests) == {0.005}  # half the interval
    await conn.disconnect()
    calls = len(port.fresh_requests)
    await asyncio.sleep(0.05)
    assert len(port.fresh_requests) == calls  # cancelled on disconnect


async def test_mammotion_refresh_loop_survives_failures_and_can_be_off() -> None:
    port = FakeMammotionPort()
    failures: list[float] = []

    async def failing(max_age_s: float) -> None:
        failures.append(max_age_s)
        raise RuntimeError("cloud hiccup")

    port.ensure_fresh = failing  # type: ignore[method-assign]
    conn = MammotionBase(port, config(), refresh_interval_s=0.01)
    assert conn.refresh_interval_s == 0.01
    await conn.connect()
    for _ in range(200):
        if len(failures) >= 2:
            break
        await asyncio.sleep(0.01)
    assert len(failures) >= 2  # the loop kept going after the first failure
    await conn.disconnect()
    off = MammotionBase(FakeMammotionPort(), config())
    assert off.refresh_interval_s == 0
    await off.connect()
    assert off._refresher is None
    await off.disconnect()
    with pytest.raises(ValueError, match="refresh_interval_s"):
        MammotionBase(FakeMammotionPort(), config(), refresh_interval_s=-1)


async def test_mammotion_cloud_refreshes_every_refresh_seconds_by_default() -> None:
    conn, _ = cloud()
    assert conn.refresh_interval_s == 20
    conn2, _ = cloud(refresh_seconds=60)
    assert conn2.refresh_interval_s == 60


async def _wait_for_requests(port: FakeMammotionPort, count: int, timeout_s: float = 3.0) -> None:
    async with asyncio.timeout(timeout_s):
        while len(port.fresh_requests) < count:  # noqa: ASYNC110
            await asyncio.sleep(0.005)


async def test_mammotion_refresh_idle_interval_while_docked() -> None:
    port = FakeMammotionPort()
    port.set_mode(WorkMode.MODE_READY)
    conn = MammotionBase(port, config(), refresh_interval_s=0.2, active_refresh_interval_s=0.01)
    await conn.connect()
    await asyncio.sleep(0.45)
    await conn.disconnect()
    assert 1 <= len(port.fresh_requests) <= 3  # ~0.2 s apart, not 0.01 s
    assert set(port.fresh_requests) == {0.1}


async def test_mammotion_refresh_active_interval_while_mowing() -> None:
    port = FakeMammotionPort()
    port.set_mode(WorkMode.MODE_WORKING)
    conn = MammotionBase(port, config(), refresh_interval_s=5.0, active_refresh_interval_s=0.01)
    await conn.connect()
    await _wait_for_requests(port, 3)
    await conn.disconnect()
    assert set(port.fresh_requests) == {0.005}  # half the active interval


async def test_mammotion_refresh_transition_shortens_and_lengthens_the_wait() -> None:
    port = FakeMammotionPort()
    port.set_mode(WorkMode.MODE_READY)
    conn = MammotionBase(port, config(), refresh_interval_s=30.0, active_refresh_interval_s=0.02)
    await conn.connect()
    await asyncio.sleep(0.1)
    assert port.fresh_requests == []  # docked: the 30 s wait is running
    port.set_mode(WorkMode.MODE_WORKING)
    await port.emit()  # the start is reported: the running wait is cut short
    await _wait_for_requests(port, 2)
    port.set_mode(WorkMode.MODE_READY)
    await port.emit()  # back on the dock: the idle interval applies again
    await asyncio.sleep(0.05)
    seen = len(port.fresh_requests)
    await asyncio.sleep(0.2)
    assert len(port.fresh_requests) == seen
    await conn.disconnect()


async def test_mammotion_refresh_active_interval_never_exceeds_the_idle_one() -> None:
    port = FakeMammotionPort()
    conn = MammotionBase(port, config(), refresh_interval_s=10.0, active_refresh_interval_s=30.0)
    assert conn.active_refresh_interval_s == 30.0
    port.set_mode(WorkMode.MODE_WORKING)
    await conn.connect()
    assert conn._current_refresh_interval_s() == 10.0
    await conn.disconnect()
    with pytest.raises(ValueError, match="active_refresh_interval_s"):
        MammotionBase(FakeMammotionPort(), config(), active_refresh_interval_s=0)


async def test_mammotion_cloud_active_refresh_comes_from_the_option() -> None:
    conn, _ = cloud()
    assert conn.active_refresh_interval_s == 5
    conn2, _ = cloud(refresh_seconds_active=2)
    assert conn2.active_refresh_interval_s == 2
    ble_conn, _ = ble()
    assert ble_conn.refresh_interval_s == 0  # the BLE bridge has no refresh loop


async def _wait_for_streams(port: FakeMammotionPort, count: int, timeout_s: float = 3.0) -> None:
    async with asyncio.timeout(timeout_s):
        while len(port.report_stream_requests) < count:  # noqa: ASYNC110
            await asyncio.sleep(0.005)


async def test_mammotion_report_stream_is_renewed_while_mowing() -> None:
    port = FakeMammotionPort()
    port.set_mode(WorkMode.MODE_WORKING)
    conn = MammotionBase(
        port, config(), refresh_interval_s=30.0, active_refresh_interval_s=5.0, report_stream_s=0.04
    )
    assert conn.report_stream_s == 0.04
    await conn.connect()
    await _wait_for_streams(port, 3)
    await conn.disconnect()
    assert set(port.report_stream_requests) == {40}  # the window, in ms
    assert port.fresh_requests == []  # no one-shots while streaming


async def test_mammotion_report_stream_is_not_started_while_docked() -> None:
    port = FakeMammotionPort()
    port.set_mode(WorkMode.MODE_READY)
    conn = MammotionBase(
        port,
        config(),
        refresh_interval_s=0.02,
        active_refresh_interval_s=0.01,
        report_stream_s=0.04,
    )
    await conn.connect()
    await asyncio.sleep(0.15)
    await conn.disconnect()
    assert port.report_stream_requests == []
    assert port.fresh_requests  # the idle one-shots


async def test_mammotion_report_stream_stops_renewing_after_docking() -> None:
    port = FakeMammotionPort()
    port.set_mode(WorkMode.MODE_WORKING)
    conn = MammotionBase(
        port, config(), refresh_interval_s=30.0, active_refresh_interval_s=5.0, report_stream_s=0.04
    )
    await conn.connect()
    await _wait_for_streams(port, 2)
    port.set_mode(WorkMode.MODE_READY)
    await port.emit()
    await asyncio.sleep(0.05)
    seen = len(port.report_stream_requests)
    await asyncio.sleep(0.2)
    assert len(port.report_stream_requests) == seen  # the window is left to expire, no stop command
    assert all(c.name != "stop_report_stream" for c in port.sent)
    await conn.disconnect()


async def test_mammotion_report_stream_off_falls_back_to_one_shots() -> None:
    port = FakeMammotionPort()
    port.set_mode(WorkMode.MODE_WORKING)
    conn = MammotionBase(
        port, config(), refresh_interval_s=30.0, active_refresh_interval_s=0.01, report_stream_s=0
    )
    await conn.connect()
    await _wait_for_requests(port, 3)
    await conn.disconnect()
    assert port.report_stream_requests == []
    with pytest.raises(ValueError, match="report_stream_s"):
        MammotionBase(FakeMammotionPort(), config(), report_stream_s=-1)


async def test_mammotion_cloud_report_stream_comes_from_the_option() -> None:
    conn, _ = cloud()
    assert conn.report_stream_s == 60
    assert cloud(report_stream_seconds=0)[0].report_stream_s == 0
    assert ble()[0].report_stream_s == 0


async def test_mammotion_job_command_boosts_reports_before_the_state_changes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(mammotion_base, "BOOST_POLL_S", 0.02)
    port = FakeMammotionPort()
    port.cooperative = False  # the real mower reports the new mode a few seconds later
    port.set_mode(WorkMode.MODE_READY)
    conn = MammotionBase(
        port, config(), refresh_interval_s=30.0, active_refresh_interval_s=5.0, report_stream_s=60
    )
    await conn.connect()
    await asyncio.sleep(0.1)
    assert port.report_stream_requests == [] and port.refreshes == 0  # docked, no commands
    assert port.fresh_requests == []
    await conn.dispatch(ResumeMow())
    await _wait_for_streams(port, 3)  # still docked: the stream call is repeated with the polls
    assert port.refreshes >= 2  # undebounced polls
    assert set(port.report_stream_requests) == {60_000}
    assert conn._last_state is not None and conn._last_state.activity is MowerActivity.DOCKED
    port.set_mode(WorkMode.MODE_WORKING)
    await port.emit()  # first active state: the stream window is renewed at once
    seen = len(port.report_stream_requests)
    await asyncio.sleep(0.1)
    assert len(port.report_stream_requests) == seen + 1
    refreshes = port.refreshes
    await asyncio.sleep(0.1)
    assert port.refreshes == refreshes  # active: no more fast polls
    await conn.disconnect()


async def test_mammotion_boost_ends_and_is_inert_without_a_refresh_loop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(mammotion_base, "BOOST_POLL_S", 0.01)
    monkeypatch.setattr(mammotion_base, "BOOST_MIN_S", 0.1)
    port = FakeMammotionPort()
    conn = MammotionBase(port, config(), refresh_interval_s=30.0)  # stream off
    await conn.connect()
    conn.boost_refresh()
    await asyncio.sleep(0.3)
    polls = port.refreshes
    assert polls >= 3 and port.report_stream_requests == []
    await asyncio.sleep(0.2)
    assert port.refreshes == polls  # the boost ended
    await conn.disconnect()
    off = MammotionBase(FakeMammotionPort(), config())
    await off.connect()
    off.boost_refresh()
    await asyncio.sleep(0.05)
    assert off.port.refreshes == 0  # type: ignore[attr-defined]
    await off.disconnect()


async def test_mammotion_state_mapping_errors_are_logged_not_raised(
    caplog: pytest.LogCaptureFixture,
) -> None:
    conn, port = cloud()
    await conn.connect()
    port.device().report_data.dev.battery_val = 1000  # clamped, still maps
    await port.emit()
    await port.close()  # device() now raises PortClosedError inside the listener
    await conn._on_device_update()  # the listener of a closed port only logs
    assert "cannot map the state" in caplog.text


async def test_mammotion_capabilities_over_cloud() -> None:
    conn, _ = cloud()
    await conn.connect()
    caps = await conn.capabilities()
    assert caps.manual_drive is False and caps.manual_drive_blades is False
    assert caps.model == "LUBA 2 AWD 5000X"
    assert caps.blade_height_range_mm == (25, 70)
    assert (caps.min_speed_mps, caps.max_speed_mps) == (0.2, 0.6)
    assert "random_angle" in caps.native_patterns
    with pytest.raises(CommandRejected, match="not available over cloud"):
        await conn.start_drive_session()


async def test_mammotion_get_map_syncs_once_then_uses_the_cache() -> None:
    ticks = iter([0.0, 1.0, 2.0, 400.0, 401.0])
    conn, port = cloud()
    conn._monotonic = lambda: next(ticks)
    await conn.connect()
    first = await conn.get_map()
    assert port.map_syncs == 1
    assert first.area_ids == [FRONT, str(BACK_HASH)]
    await conn.get_map()
    assert port.map_syncs == 1  # fresh and complete: no new sync
    await conn.get_map()
    assert port.map_syncs == 2  # older than MAP_MAX_AGE_S


async def test_mammotion_get_map_sync_timeout_and_link_loss() -> None:
    conn, port = cloud()
    await conn.connect()
    port.map_sync_error = TimeoutError("slow")
    mower_map = await conn.get_map()  # what arrived is still returned
    assert mower_map.area_ids
    port.map_sync_error = NoTransportAvailableError("down")
    conn._map_synced_at = None
    with pytest.raises(NotConnected):
        await conn.get_map()


async def test_mammotion_start_mow_sequence_in_mammotion_ha_order() -> None:
    conn, port = cloud()
    await conn.connect()
    outcome = await conn.execute(
        StartMow(area_ids=[FRONT], pattern={"name": "checkerboard", "angle_deg": 20})
    )
    assert outcome.status is CommandStatus.EXECUTED
    assert outcome.reason == "mower is mowing"
    assert port.names() == ["generate_route_information", "start_job"]
    plan, start = port.sent
    assert plan.expected_field == "bidire_reqconver_path"
    assert plan.via == "send_and_wait"  # the idempotent route request may be re-sent
    assert start.via == "send"  # start_job once, no reply awaited, never re-sent
    assert plan.priority is Priority.USER and start.priority is Priority.USER
    route = plan.kwargs["generate_route_information"]
    assert route.one_hashs == [FRONT_HASH]
    assert (route.channel_mode, route.toward, route.toward_included_angle) == (1, 20, 90)
    assert route.blade_height == 55  # current height from the report
    assert route.channel_width == 25


async def test_mammotion_start_mow_ends_a_paused_job_first() -> None:
    conn, port = cloud()
    await conn.connect()
    port.set_mode(WorkMode.MODE_PAUSE)
    await conn.execute(SetBladeHeight(mm=40))
    await conn.execute(SetSpeed(mps=0.5))
    outcome = await conn.execute(StartMow(area_ids=[FRONT]))
    assert outcome.status is CommandStatus.EXECUTED
    assert port.names()[-3:] == ["cancel_job", "generate_route_information", "start_job"]
    route = port.sent[-2].kwargs["generate_route_information"]
    assert route.blade_height == 40 and route.speed == 0.5


@pytest.mark.parametrize(
    ("command", "match"),
    [
        (StartMow(plan_ref="weekly"), "stored plans"),
        (StartMow(area_ids=["12345"]), "unknown area ids"),
        (StartMow(area_ids=["front"]), "unknown area ids"),
        (StartMow(area_ids=[FRONT], pattern={"name": "spiral"}), "not supported natively"),
        (StartMow(area_ids=[FRONT], pattern={"name": "stripes", "spacing_m": 1.0}), "spacing_m"),
    ],
)
async def test_mammotion_start_mow_rejections(command: StartMow, match: str) -> None:
    conn, port = cloud()
    await conn.connect()
    with pytest.raises(CommandRejected, match=match):
        await conn.execute(command)
    assert port.sent == []


def _spot(native: SpotNativeJob | None = None) -> SpotMowPlan:
    return SpotMowPlan.build(
        [SpotSegment.from_points([(1, 1), (3, 1)], blades_on=True, speed_mps=0.4)],
        ["stripes"],
        native=native,
    )


async def test_mammotion_rejects_spot_plans_without_a_native_job() -> None:
    conn, port = cloud()
    await conn.connect()
    with pytest.raises(CommandRejected, match="custom paths not supported"):
        await conn.execute(StartMow(spot_plan=_spot()))
    assert port.sent == []


async def test_mammotion_runs_the_native_job_of_a_spot_plan() -> None:
    conn, port = cloud()
    await conn.connect()
    native = SpotNativeJob(area_ids=[FRONT], pattern={"name": "stripes", "angle_deg": 30})
    outcome = await conn.execute(StartMow(spot_plan=_spot(native)))
    assert outcome.status is CommandStatus.EXECUTED
    assert port.names()[-2:] == ["generate_route_information", "start_job"]


@pytest.mark.parametrize("mode", [WorkMode.MODE_PAUSE, WorkMode.MODE_WORKING])
async def test_mammotion_return_with_end_job_cancels_the_job_first(mode: WorkMode) -> None:
    conn, port = cloud()
    await conn.connect()
    port.set_mode(mode)
    outcome = await conn.execute(ReturnToDock(end_job=True))
    assert outcome.status is CommandStatus.EXECUTED
    assert port.names() == ["cancel_job", "return_to_dock"]
    assert all(c.priority is Priority.USER and c.via == "send" for c in port.sent)
    assert port.device().report_data.dev.sys_status == int(WorkMode.MODE_RETURNING)


async def test_mammotion_return_without_end_job_keeps_the_job() -> None:
    conn, port = cloud()
    await conn.connect()
    port.set_mode(WorkMode.MODE_PAUSE)
    await conn.execute(ReturnToDock())
    assert port.names() == ["return_to_dock"]


async def test_mammotion_start_mow_while_mowing_is_rejected() -> None:
    conn, port = cloud()
    await conn.connect()
    port.set_mode(WorkMode.MODE_WORKING)
    with pytest.raises(CommandRejected, match="mowing"):
        await conn.execute(StartMow(area_ids=[FRONT]))


@pytest.mark.parametrize(
    ("command", "name", "mode"),
    [
        (PauseMow(), "pause_execute_task", WorkMode.MODE_PAUSE),
        (ResumeMow(), "resume_execute_task", WorkMode.MODE_WORKING),
        (ReturnToDock(), "return_to_dock", WorkMode.MODE_RETURNING),
    ],
)
async def test_mammotion_job_commands_are_confirmed_by_the_work_mode(
    command: PauseMow | ResumeMow | ReturnToDock, name: str, mode: WorkMode
) -> None:
    conn, port = cloud()
    await conn.connect()
    outcome = await conn.execute(command)
    assert outcome.status is CommandStatus.EXECUTED
    assert port.names() == [name]
    assert port.sent[0].priority is Priority.USER
    assert port.device().report_data.dev.sys_status == int(mode)


async def test_mammotion_unconfirmed_command_is_accepted_not_executed() -> None:
    conn, port = cloud(confirm_timeout_s=2)
    port.cooperative = False
    await conn.connect()
    outcome = await conn.execute(PauseMow())
    assert outcome.status is CommandStatus.ACCEPTED
    assert "did not report paused within 2 s" in (outcome.reason or "")
    port.set_mode(WorkMode.MODE_READY)  # docked counts as arrived for ReturnToDock
    assert (await conn.execute(ReturnToDock())).status is CommandStatus.EXECUTED


async def test_mammotion_blade_height_and_speed_are_sent() -> None:
    conn, port = cloud()
    await conn.connect()
    assert (await conn.execute(SetBladeHeight(mm=45))).status is CommandStatus.ACCEPTED
    assert (await conn.execute(SetSpeed(mps=0.6))).status is CommandStatus.ACCEPTED
    assert [(c.name, c.kwargs) for c in port.sent] == [
        ("set_blade_height", {"height": 45}),
        ("set_speed", {"speed": 0.6}),
    ]


async def test_mammotion_emergency_stop_uses_emergency_priority() -> None:
    conn, port = cloud()
    await conn.connect()
    port.set_mode(WorkMode.MODE_WORKING)
    outcome = await conn.execute(EmergencyStop())
    assert outcome.status is CommandStatus.ACCEPTED
    assert port.names() == ["pause_execute_task", "send_movement", "operate_on_device"]
    assert {c.priority for c in port.sent} == {Priority.EMERGENCY}
    assert port.sent[1].kwargs == {"linear_speed": 0, "angular_speed": 0}
    assert port.sent[2].kwargs["cut_knife_ctrl"] == 0 and port.sent[2].kwargs["main_ctrl"] == 0


async def test_mammotion_emergency_stop_tries_every_step() -> None:
    conn, port = cloud()
    await conn.connect()
    port.fail["pause_execute_task"] = TransportError("refused")
    outcome = await conn.execute(EmergencyStop())
    assert outcome.status is CommandStatus.ACCEPTED
    assert "failed: pause_execute_task: TransportError" in (outcome.reason or "")
    assert port.names() == ["send_movement", "operate_on_device"]
    port.fail = {n: TransportError("x") for n in [*port.names(), "pause_execute_task"]}
    assert (await conn.execute(EmergencyStop())).status is CommandStatus.FAILED
    port.link = False
    with pytest.raises(NotConnected):
        await conn.execute(EmergencyStop())


async def test_mammotion_emergency_stop_runs_even_during_an_error() -> None:
    conn, port = cloud()
    await conn.connect()
    port.set_mode(WorkMode.MODE_SYSTEM_ERROR)
    with pytest.raises(SensorStop):
        await conn.execute(PauseMow())
    assert (await conn.execute(EmergencyStop())).status is CommandStatus.ACCEPTED


async def test_mammotion_start_mow_unconfirmed_is_accepted_and_sent_once() -> None:
    conn, port = cloud(confirm_timeout_s=2)
    port.cooperative = False  # the mower never reports mowing (or the report is late)
    await conn.connect()
    outcome = await conn.execute(StartMow(area_ids=[FRONT]))
    assert outcome.status is CommandStatus.ACCEPTED
    assert outcome.reason == "command sent; the mower did not report mowing within 2 s"
    assert port.names() == ["generate_route_information", "start_job"]
    assert [c.via for c in port.sent] == ["send_and_wait", "send"]


async def test_mammotion_start_mow_dispatch_sends_and_confirm_only_waits() -> None:
    conn, port = cloud(confirm_timeout_s=5)
    port.cooperative = False
    port.wait_real = True
    await conn.connect()
    dispatch = await conn.dispatch(StartMow(area_ids=[FRONT]))
    assert dispatch.confirm is not None and dispatch.outcome is None
    assert dispatch.sent.startswith("start_job sent")
    assert port.names() == ["generate_route_information", "start_job"]
    waiting = asyncio.create_task(dispatch.settle())
    await asyncio.sleep(0.05)
    assert not waiting.done()
    port.set_mode(WorkMode.MODE_WORKING)  # Telemetry showed mowing 2 s after the start
    port.device().report_data.dev.charge_state = 0
    await port.emit()
    outcome = await asyncio.wait_for(waiting, 1)
    assert outcome.status is CommandStatus.EXECUTED
    jobs = [n for n in port.names() if not _is_info_request(n)]
    assert jobs == ["generate_route_information", "start_job"]  # nothing re-sent


async def test_mammotion_start_mow_confirmation_agrees_with_the_state_mapping() -> None:
    conn, port = cloud(confirm_timeout_s=1)
    port.cooperative = False
    await conn.connect()
    port.set_mode(WorkMode.MODE_READY)
    dispatch = await conn.dispatch(StartMow(area_ids=[FRONT]))
    for mode in WorkMode:
        port.set_mode(mode)
        port.device().report_data.dev.charge_state = 0
        state = await conn.get_state()
        outcome = await dispatch.settle()
        mowing = state.activity is MowerActivity.MOWING
        assert (outcome.status is CommandStatus.EXECUTED) == mowing, mode


async def test_mammotion_cancelled_confirmation_sends_nothing() -> None:
    conn, port = cloud(confirm_timeout_s=30)
    port.cooperative = False
    port.wait_real = True
    await conn.connect()
    dispatch = await conn.dispatch(StartMow(area_ids=[FRONT]))
    waiting = asyncio.create_task(dispatch.settle())
    await asyncio.sleep(0.05)
    waiting.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiting
    jobs = [n for n in port.names() if not _is_info_request(n)]
    assert jobs == ["generate_route_information", "start_job"]


async def test_mammotion_pause_preempts_an_unconfirmed_start_through_the_controller() -> None:
    conn, port = cloud(confirm_timeout_s=30)
    port.wait_real = True
    port.cooperative = False
    issuer = PrincipalRef(subject=uuid4(), kind=PrincipalKind.USER)

    def envelope(command: StartMow | PauseMow) -> CommandEnvelope:
        return CommandEnvelope.create(
            mower_id=MOWER_ID,
            account_id=ACCOUNT_ID,
            issued_by=issuer,
            command=command,
            ttl=timedelta(seconds=30),
        )

    async with MowerController(conn, mower_id=MOWER_ID) as controller:
        start = asyncio.create_task(controller.handle(envelope(StartMow(area_ids=[FRONT]))))
        await asyncio.sleep(0.05)
        assert not start.done()  # waiting for mowing, outside the queue
        port.cooperative = True
        pause = await asyncio.wait_for(controller.handle(envelope(PauseMow())), 1)
        assert pause.status is CommandStatus.EXECUTED
        started = await asyncio.wait_for(start, 1)
        assert started.status is CommandStatus.ACCEPTED
        assert "confirmation interrupted by pause_mow" in (started.reason or "")
    commands = [n for n in port.names() if not _is_info_request(n)]
    assert commands == ["generate_route_information", "start_job", "pause_execute_task"]


async def test_mammotion_transport_errors_map_to_outcomes() -> None:
    conn, port = cloud()
    await conn.connect()
    port.fail["generate_route_information"] = CommandTimeoutError("bidire_reqconver_path", 2)
    outcome = await conn.execute(StartMow(area_ids=[FRONT]))
    assert outcome.status is CommandStatus.FAILED
    assert "did not answer" in (outcome.reason or "")
    assert "start_job" not in port.names()  # no route, no start
    port.fail = {"start_job": TransportError("refused")}
    assert (await conn.execute(StartMow(area_ids=[FRONT]))).status is CommandStatus.FAILED
    port.fail["set_speed"] = TransportError("rate limited")
    assert (await conn.execute(SetSpeed(mps=0.5))).status is CommandStatus.FAILED
    port.link = False
    with pytest.raises(NotConnected):
        await conn.execute(PauseMow())


async def test_mammotion_cloud_through_the_controller() -> None:
    conn, port = cloud()
    async with MowerController(conn, mower_id=MOWER_ID) as controller:
        envelope = CommandEnvelope.create(
            mower_id=MOWER_ID,
            account_id=ACCOUNT_ID,
            issued_by=PrincipalRef(subject=uuid4(), kind=PrincipalKind.USER),
            command=StartMow(area_ids=[FRONT], pattern={"name": "stripes", "angle_deg": 45}),
            ttl=timedelta(seconds=30),
        )
        result = await controller.handle(envelope)
        assert result.status is CommandStatus.EXECUTED, result.reason
        stop = CommandEnvelope.create(
            mower_id=MOWER_ID,
            account_id=ACCOUNT_ID,
            issued_by=PrincipalRef(subject=uuid4(), kind=PrincipalKind.USER),
            command=EmergencyStop(),
            ttl=timedelta(seconds=10),
        )
        assert (await controller.handle(stop)).status is CommandStatus.ACCEPTED
    assert port.map_syncs == 1
    assert [n for n in port.names() if not _is_info_request(n)] == [
        "generate_route_information",
        "start_job",
        "pause_execute_task",
        "send_movement",
        "operate_on_device",
    ]


async def test_mammotion_cloud_requests_the_device_information_on_connect() -> None:
    conn, port = cloud()
    conn._device_info_retry_s = 0.01
    await conn.connect()
    await asyncio.sleep(0.1)
    info = [c for c in port.sent if c.name.startswith("get_device_")]
    assert [c.name for c in info] == [
        "get_device_version_info",
        "get_device_base_info",
        "get_device_product_model",
    ]
    assert {c.priority for c in info} == {Priority.BACKGROUND}
    await conn.disconnect()
    # firmware and sub model known: no retry
    assert len([c for c in port.sent if c.name.startswith("get_device_")]) == 3


async def test_mammotion_device_information_retries_only_the_missing_firmware() -> None:
    port = FakeMammotionPort(firmware="")
    conn, _ = cloud(port)
    conn._device_info_retry_s = 0.01
    await conn.connect()
    await asyncio.sleep(0.2)
    names = port.names()
    assert names.count("get_device_version_info") == mammotion_base.DEVICE_INFO_ATTEMPTS
    assert names.count("get_device_base_info") == mammotion_base.DEVICE_INFO_ATTEMPTS
    assert names.count("get_device_product_model") == 1  # the sub model is known
    await conn.disconnect()


async def test_mammotion_device_information_retries_the_product_model_until_answered() -> None:
    port = FakeMammotionPort(device=sample_device(sub_model_id=""))
    conn, _ = cloud(port)
    conn._device_info_retry_s = 0.01
    await conn.connect()
    await asyncio.sleep(0.02)
    caps = await conn.capabilities()
    assert caps.blade_height_range_mm is None and caps.max_speed_mps is None
    port.product_model = ("LubaVP", "HM030070LB2PAWD50OMNI")  # the mower answers now
    await asyncio.sleep(0.2)
    names = port.names()
    assert names.count("get_device_version_info") == 1  # firmware known: never repeated
    assert 2 <= names.count("get_device_product_model") < mammotion_base.DEVICE_INFO_ATTEMPTS
    caps = await conn.capabilities()
    assert caps.model == "LUBA 2 AWD 5000X"
    assert caps.blade_height_range_mm == (25, 70)
    assert (caps.min_speed_mps, caps.max_speed_mps) == (0.2, 0.6)
    state = await conn.get_state()
    assert state.raw["model"] == "LUBA 2 AWD 5000X"
    await conn.disconnect()


async def test_mammotion_device_information_retries_are_bounded() -> None:
    port = FakeMammotionPort(device=sample_device(sub_model_id=""), firmware="")
    conn, _ = cloud(port)
    conn._device_info_retry_s = 0.01
    await conn.connect()
    await asyncio.sleep(0.3)
    names = [n for n in port.names() if n.startswith("get_device_")]
    assert len(names) == 3 * mammotion_base.DEVICE_INFO_ATTEMPTS
    await conn.disconnect()


async def test_mammotion_device_information_stops_on_disconnect() -> None:
    port = FakeMammotionPort(firmware="")
    conn, _ = cloud(port)
    conn._device_info_retry_s = 0.05
    await conn.connect()
    await asyncio.sleep(0.01)
    await conn.disconnect()
    await asyncio.sleep(0.1)
    assert port.names().count("get_device_version_info") == 1


async def test_mammotion_device_information_failures_are_ignored() -> None:
    port = FakeMammotionPort(firmware="")
    port.fail["get_device_version_info"] = TransportError("offline")
    port.fail["get_device_product_model"] = TransportError("offline")
    conn, _ = cloud(port)
    conn._device_info_retry_s = 0.01
    await conn.connect()
    await asyncio.sleep(0.2)
    assert port.names().count("get_device_base_info") == mammotion_base.DEVICE_INFO_ATTEMPTS
    assert conn.is_connected
    await conn.disconnect()


async def test_mammotion_ble_requests_only_the_dock_buffer_and_the_light() -> None:
    conn, port = ble()
    await conn.connect()
    await asyncio.sleep(0.01)
    assert [(c.name, c.kwargs) for c in port.sent] == [
        ("read_write_device", DOCK_ARGS),
        (LIGHT_READ_COMMAND, {"ids": 1126}),
    ]
    await conn.disconnect()


async def test_mammotion_cloud_requests_the_dock_buffer_with_the_device_information() -> None:
    port = FakeMammotionPort(device=sample_device(with_dock=False))
    conn, _ = cloud(port)
    conn._device_info_retry_s = 0.01
    await conn.connect()
    await asyncio.sleep(0.1)
    requests = [c for c in port.sent if _is_info_request(c.name)]
    assert [c.name for c in requests] == [
        "get_device_version_info",
        "get_device_base_info",
        "get_device_product_model",
        "read_write_device",
        LIGHT_READ_COMMAND,
    ]
    assert requests[-2].kwargs == DOCK_ARGS
    assert requests[-2].priority is Priority.BACKGROUND
    # the fake mower answered with the buffer: dock and origin known, no retry
    assert port.names().count("read_write_device") == 1
    mower_map = await conn.get_map()
    assert mower_map.dock is not None
    assert mower_map.reference.rotation_deg == pytest.approx(RTK_YAW_DEG)
    state = await conn.get_state()
    assert state.position_source is PositionSource.DOCK
    await conn.disconnect()


async def test_mammotion_dock_buffer_is_retried_until_the_dock_is_known() -> None:
    port = FakeMammotionPort(device=sample_device(with_dock=False))
    port.dock_answer = None  # the mower does not answer yet
    conn, _ = cloud(port)
    conn._device_info_retry_s = 0.02
    await conn.connect()
    watch = conn.watch_state()
    first = await anext(watch)
    assert first.position_source is PositionSource.RTK  # raw position, dock unknown
    await asyncio.sleep(0.03)
    assert port.names().count("read_write_device") >= 2
    await port.deliver_dock_buffer()  # the answer arrives
    update = await asyncio.wait_for(anext(watch), 1)
    while update.position_source is not PositionSource.DOCK:  # skip the light answer's update
        update = await asyncio.wait_for(anext(watch), 1)
    assert update.position_source is PositionSource.DOCK
    await asyncio.sleep(0.15)
    count = port.names().count("read_write_device")
    assert count < mammotion_base.DEVICE_INFO_ATTEMPTS
    assert port.names().count("get_device_version_info") == 1  # firmware known
    await conn.disconnect()


async def test_mammotion_dock_buffer_retries_are_bounded() -> None:
    port = FakeMammotionPort(device=sample_device(with_dock=False))
    port.dock_answer = None
    conn, _ = ble(port)
    conn._device_info_retry_s = 0.01
    await conn.connect()
    await asyncio.sleep(0.2)
    docks = [n for n in port.names() if n == "read_write_device"]
    assert docks == ["read_write_device"] * mammotion_base.DEVICE_INFO_ATTEMPTS
    assert port.names().count(LIGHT_READ_COMMAND) == 1  # answered in the first round
    await conn.disconnect()


async def test_mammotion_dock_buffer_failures_are_ignored() -> None:
    port = FakeMammotionPort(device=sample_device(with_dock=False), firmware="")
    port.fail["read_write_device"] = TransportError("offline")
    conn, _ = cloud(port)
    conn._device_info_retry_s = 0.01
    await conn.connect()
    await asyncio.sleep(0.2)
    # every round went on after the failed dock request
    assert port.names().count("get_device_version_info") == mammotion_base.DEVICE_INFO_ATTEMPTS
    assert conn.is_connected
    assert (await conn.get_map()).dock is None
    await conn.disconnect()


async def test_mammotion_dock_buffer_is_requested_again_after_reconnect() -> None:
    port = FakeMammotionPort(device=sample_device(with_dock=False))
    port.dock_answer = None
    conn, _ = ble(port)
    conn._device_info_retry_s = 10.0
    await conn.connect()
    await asyncio.sleep(0.01)
    await conn.disconnect()
    await conn.connect()
    await asyncio.sleep(0.01)
    assert port.names().count("read_write_device") == 2
    await conn.disconnect()


async def test_mammotion_map_waits_for_the_dock_buffer() -> None:
    port = FakeMammotionPort(device=sample_device(with_dock=False))
    port.dock_answer = None
    conn, _ = ble(port)
    conn._device_info_retry_s = 10.0
    await conn.connect()
    await asyncio.sleep(0.01)
    assert (await conn.get_map()).dock is None  # no answer: the map stays without a dock
    assert port.names().count("read_write_device") == 2  # connect and the map read
    port.dock_answer = dock_buffer()
    mower_map = await conn.get_map()
    assert mower_map.dock is not None
    assert mower_map.reference.rotation_deg == pytest.approx(RTK_YAW_DEG)
    calls = port.names().count("read_write_device")
    await conn.get_map()  # dock known: no further request
    assert port.names().count("read_write_device") == calls
    await conn.disconnect()


async def test_mammotion_state_carries_firmware_and_model_and_republishes_on_change() -> None:
    port = FakeMammotionPort(firmware="", model="LubaAWD1000723")
    conn, _ = cloud(port)
    await conn.connect()
    state = await conn.get_state()
    assert state.firmware is None
    assert state.raw["model"] == "LubaAWD1000723"
    assert (await conn.capabilities()).model == "LubaAWD1000723"
    watch = conn.watch_state()
    assert (await anext(watch)).firmware is None
    port.set_firmware("1.30.29.26")
    await port.emit()
    update = await asyncio.wait_for(anext(watch), 1)
    assert update.firmware == "1.30.29.26"
    await conn.disconnect()


async def test_mammotion_current_map_rebuilds_without_a_sync() -> None:
    port = FakeMammotionPort(device=sample_device(with_dock=False))
    port.dock_answer = None
    conn, _ = ble(port)
    conn._device_info_retry_s = 10.0
    await conn.connect()
    assert isinstance(conn, MapRebuildable)
    assert conn.current_map() is None  # nothing loaded yet
    assert (await conn.get_map()).dock is None
    syncs = port.map_syncs
    await port.deliver_dock_buffer()  # the answer arrives after the map read
    rebuilt = conn.current_map()
    assert rebuilt is not None and rebuilt.dock is not None
    assert rebuilt.reference.rotation_deg == pytest.approx(RTK_YAW_DEG)
    assert port.map_syncs == syncs
    await conn.disconnect()
    assert conn.current_map() == rebuilt  # disconnected: the last map, no device access


async def test_mammotion_docked_speed_falls_back_to_the_setpoint() -> None:
    device = sample_device()
    device.report_data.work.man_run_speed = 0  # docked: the mower reports no speed
    device.mower_state.travel_speed = 0
    port = FakeMammotionPort(device=device)
    conn, _ = cloud(port)
    await conn.connect()
    state = await conn.get_state()
    assert state.speed_mps == pytest.approx(mammotion_base.DEFAULT_SPEED_MPS)
    assert state.raw["speed_source"] == "setpoint"
    await conn.execute(SetSpeed(mps=0.5))
    state = await conn.get_state()
    assert state.speed_mps == pytest.approx(0.5)
    assert state.raw["speed_source"] == "setpoint"
    port.set_mode(WorkMode.MODE_WORKING)
    device.report_data.work.man_run_speed = 40  # mowing: 0.4 m/s reported
    state = await conn.get_state()
    assert state.speed_mps == pytest.approx(0.4)
    assert state.raw["speed_source"] == "reported"
    await conn.disconnect()
