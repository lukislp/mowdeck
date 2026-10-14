"""The simulated mower: lifecycle, path, battery, RTK, sensors, driving, state stream."""

import asyncio
from collections.abc import Callable
from typing import Any
from uuid import uuid4

import pytest
from mowdeck_contracts.v1 import (
    ConnectionKind,
    DockPose,
    EmergencyStop,
    GeoReference,
    MapArea,
    MapZone,
    MowerActivity,
    MowerMap,
    MowerState,
    PauseMow,
    PositionSource,
    ResumeMow,
    ReturnToDock,
    RtkFixStatus,
    RtkSource,
    SetBladeHeight,
    SetLight,
    SetSpeed,
    SpotMowPlan,
    SpotSegment,
    StartMow,
)
from mowdeck_control import (
    CommandRejected,
    ConnectionConfig,
    NotConnected,
    SensorStop,
    SimulatedMower,
    SimulationConfig,
    create_simulated_connection,
    default_map,
    from_wgs84,
    point_in_polygon,
)
from mowdeck_control.simulator import LIFT_ERROR_CODE
from mowdeck_control.testing import ManualClock

FLOWERBED = default_map().no_go_zones[0].boundary


def _sim(**config: Any) -> SimulatedMower:
    return SimulatedMower(config=SimulationConfig(**config), clock=ManualClock())


async def _connected(**config: Any) -> SimulatedMower:
    sim = _sim(**config)
    await sim.connect()
    return sim


def _run_until(
    sim: SimulatedMower,
    predicate: Callable[[MowerState], bool],
    *,
    dt: float = 1.0,
    max_steps: int = 20_000,
) -> list[MowerState]:
    states: list[MowerState] = []
    for _ in range(max_steps):
        state = sim.tick(dt)
        states.append(state)
        if predicate(state):
            return states
    msg = "condition not reached"
    raise AssertionError(msg)


def _dedupe(values: list[MowerActivity]) -> list[MowerActivity]:
    result: list[MowerActivity] = []
    for value in values:
        if not result or result[-1] != value:
            result.append(value)
    return result


async def test_full_lifecycle_with_battery_drain_and_charge() -> None:
    sim = await _connected()
    activities = [(await sim.get_state()).activity]
    assert activities == [MowerActivity.DOCKED]
    assert (await sim.get_state()).battery_percent == 100

    outcome = await sim.execute(StartMow(area_ids=["front"]))
    assert outcome.status == "executed"
    state = await sim.get_state()
    activities.append(state.activity)
    assert state.raw["phase"] == "transfer"
    assert state.raw["blade_on"] is False

    states = _run_until(sim, lambda s: s.raw["phase"] == "mowing")
    assert states[-1].raw["blade_on"] is True
    states += _run_until(sim, lambda s: s.raw["progress_percent"] >= 30)

    assert (await sim.execute(PauseMow())).status == "executed"
    paused = await sim.get_state()
    activities.append(paused.activity)
    held = sim.tick(600)
    assert held.raw["x"] == paused.raw["x"]
    assert held.raw["y"] == paused.raw["y"]
    assert held.raw["blade_on"] is False

    assert (await sim.execute(ResumeMow())).status == "executed"
    states += _run_until(sim, lambda s: s.activity is MowerActivity.CHARGING)
    activities += [s.activity for s in states]
    lowest = min(s.battery_percent for s in states)
    assert lowest < 100
    states = _run_until(sim, lambda s: s.activity is MowerActivity.DOCKED, dt=60)
    activities += [s.activity for s in states]
    assert states[-1].battery_percent == 100
    assert states[-1].charging is False
    assert _dedupe(activities) == [
        MowerActivity.DOCKED,
        MowerActivity.MOWING,
        MowerActivity.PAUSED,
        MowerActivity.MOWING,
        MowerActivity.RETURNING,
        MowerActivity.DOCKED,
        MowerActivity.CHARGING,
        MowerActivity.DOCKED,
    ]
    assert sim.local_position == (-1.0, 1.0)


async def test_position_stays_in_area_and_out_of_no_go_zone() -> None:
    sim = await _connected()
    mower_map = await sim.get_map()
    await sim.execute(
        StartMow(area_ids=["front", "back"], pattern={"name": "stripes", "angle_deg": 0})
    )
    mowing_areas: set[str] = set()
    for _ in range(40_000):
        state = sim.tick(0.5)
        point = sim.local_position
        assert not point_in_polygon(point, FLOWERBED), point
        if state.raw["blade_on"]:
            area = mower_map.area(state.raw["area_id"])
            assert area is not None
            assert point_in_polygon(point, area.boundary), point
            mowing_areas.add(area.id)
        if state.activity is MowerActivity.DOCKED:
            break
    else:
        pytest.fail("mower did not come home")
    assert mowing_areas == {"front", "back"}


@pytest.mark.parametrize("pattern", ["diagonal", "checkerboard"])
async def test_other_native_patterns_stay_safe(pattern: str) -> None:
    sim = await _connected()
    mower_map = await sim.get_map()
    await sim.execute(StartMow(area_ids=["front"], pattern={"name": pattern, "spacing_m": 0.5}))
    for _ in range(40_000):
        state = sim.tick(0.5)
        assert not point_in_polygon(sim.local_position, FLOWERBED)
        if state.raw["blade_on"]:
            front = mower_map.area("front")
            assert front is not None
            assert point_in_polygon(sim.local_position, front.boundary)
        if state.activity is MowerActivity.DOCKED:
            break
    else:
        pytest.fail("mower did not come home")


async def test_rtk_status_reporting_and_hold_without_fix() -> None:
    sim = await _connected()
    state = await sim.get_state()
    assert state.rtk_fix is RtkFixStatus.FIXED
    assert state.rtk_source is RtkSource.BASE_STATION
    assert state.position_source is PositionSource.RTK
    sim.set_rtk(RtkFixStatus.FLOAT, RtkSource.NETWORK)
    state = await sim.get_state()
    assert (state.rtk_fix, state.rtk_source) == (RtkFixStatus.FLOAT, RtkSource.NETWORK)

    await sim.execute(StartMow(area_ids=["front"]))
    sim.tick(30)
    sim.set_rtk(RtkFixStatus.NONE)
    before = sim.local_position
    held = sim.tick(60)
    assert sim.local_position == before
    assert held.rtk_fix is RtkFixStatus.NONE
    assert held.position_source is PositionSource.GNSS
    assert held.activity is MowerActivity.MOWING
    assert held.raw["blade_on"] is False
    sim.set_rtk(RtkFixStatus.FIXED)
    sim.tick(5)
    assert sim.local_position != before


async def test_map_geo_reference_round_trip() -> None:
    sim = await _connected()
    mower_map = await sim.get_map()
    await sim.execute(StartMow(area_ids=["back"]))
    for _ in range(5):
        state = sim.tick(37)
        assert state.position is not None
        x, y = from_wgs84(mower_map, state.position.lat, state.position.lon)
        assert x == pytest.approx(sim.local_position[0], abs=1e-6)
        assert y == pytest.approx(sim.local_position[1], abs=1e-6)
        assert state.position.heading is not None


async def test_lift_stops_the_mower_and_resume_cannot_clear_it() -> None:
    sim = await _connected()
    await sim.execute(StartMow(area_ids=["front"]))
    _run_until(sim, lambda s: s.raw["blade_on"] is True)
    sim.lift()
    state = await sim.get_state()
    assert state.activity is MowerActivity.ERROR
    assert state.error_code == LIFT_ERROR_CODE
    assert state.raw["blade_on"] is False
    with pytest.raises(SensorStop, match="lifted"):
        await sim.execute(ResumeMow())
    with pytest.raises(SensorStop):
        await sim.execute(StartMow(area_ids=["back"]))
    with pytest.raises(SensorStop):
        await sim.start_drive_session()
    position = sim.local_position
    assert sim.tick(120).activity is MowerActivity.ERROR
    assert sim.local_position == position
    assert (await sim.execute(EmergencyStop())).status == "executed"
    assert (await sim.get_state()).activity is MowerActivity.ERROR

    sim.clear_error()
    state = await sim.get_state()
    assert state.activity is MowerActivity.PAUSED
    assert state.error_code is None
    assert (await sim.execute(ResumeMow())).status == "executed"
    assert (await sim.get_state()).activity is MowerActivity.MOWING


async def test_control_simulator_light_switches_in_every_state() -> None:
    sim = await _connected()
    assert (await sim.capabilities()).light is True
    assert (await sim.get_state()).light_on is False
    outcome = await sim.execute(SetLight(on=True))
    assert outcome.status == "executed"
    assert (await sim.get_state()).light_on is True
    sim.lift()
    assert (await sim.execute(SetLight(on=False))).status == "executed"
    assert (await sim.get_state()).light_on is False
    sim.clear_error()
    sim.inject_stuck()
    assert (await sim.execute(SetLight(on=True))).status == "executed"
    assert (await sim.get_state()).light_on is True


async def test_control_simulator_without_light() -> None:
    sim = await _connected(light=False)
    assert (await sim.capabilities()).light is False
    assert (await sim.get_state()).light_on is None
    outcome = await sim.execute(SetLight(on=True))
    assert outcome.status == "rejected"


async def test_control_simulator_lift_sets_the_raw_lifted_flag() -> None:
    sim = await _connected()
    assert "lifted" not in (await sim.get_state()).raw
    sim.inject_error("E42")
    assert "lifted" not in (await sim.get_state()).raw
    sim.clear_error()
    sim.lift()
    assert (await sim.get_state()).raw["lifted"] is True
    sim.clear_error()
    assert "lifted" not in (await sim.get_state()).raw


async def test_error_injection_and_clear() -> None:
    sim = await _connected()
    sim.clear_error()  # no error: no-op
    sim.inject_error("E42")
    state = await sim.get_state()
    assert (state.activity, state.error_code) == (MowerActivity.ERROR, "E42")
    assert state.error_message == "simulated error E42"
    with pytest.raises(SensorStop, match="E42"):
        await sim.execute(ReturnToDock())
    sim.clear_error()
    assert (await sim.get_state()).activity is MowerActivity.DOCKED

    session = await sim.start_drive_session()
    await session.update(1, 0, False)
    sim.tick(4)
    await session.close()
    sim.inject_error("E7", "wheel motor")
    assert (await sim.get_state()).error_message == "wheel motor"
    sim.clear_error()
    assert (await sim.get_state()).activity is MowerActivity.IDLE


async def test_low_battery_sends_the_mower_home() -> None:
    sim = await _connected(min_start_battery_percent=16)
    sim.set_battery(10)
    outcome = await sim.execute(StartMow(area_ids=["front"]))
    assert outcome.status == "rejected"
    assert outcome.reason == "battery too low to start"
    sim.set_battery(16)
    await sim.execute(StartMow(area_ids=["front"]))
    states = _run_until(sim, lambda s: s.activity is MowerActivity.RETURNING)
    assert states[-1].battery_percent <= 15
    assert "job_areas" not in states[-1].raw
    _run_until(sim, lambda s: s.activity is MowerActivity.DOCKED)


async def test_commands_and_validation() -> None:
    sim = await _connected()
    assert (await sim.execute(PauseMow())).status == "rejected"
    assert (await sim.execute(ResumeMow())).status == "rejected"
    assert (await sim.execute(ReturnToDock())).reason == "already docked"
    assert (await sim.execute(SetBladeHeight(mm=110))).status == "rejected"
    assert (await sim.execute(SetBladeHeight(mm=55))).status == "executed"
    assert (await sim.execute(SetSpeed(mps=1.5))).status == "rejected"
    assert (await sim.execute(SetSpeed(mps=0.05))).status == "rejected"  # below min_speed_mps
    assert (await sim.execute(SetSpeed(mps=0.8))).status == "executed"
    state = await sim.get_state()
    assert state.blade_height_mm == 55
    assert (await sim.execute(StartMow(plan_ref="p1"))).status == "rejected"
    unknown = await sim.execute(StartMow(area_ids=["nowhere"]))
    assert unknown.reason == "unknown area ids: nowhere"
    edge = await sim.execute(StartMow(area_ids=["front"], pattern={"name": "edge_only"}))
    assert edge.status == "rejected"
    assert "edge_only" in (edge.reason or "")
    bad = await sim.execute(StartMow(area_ids=["front"], pattern={"spacing_m": 9}))
    assert bad.status == "rejected"

    await sim.execute(StartMow(area_ids=["front"]))
    sim.tick(60)
    assert (await sim.get_state()).speed_mps == pytest.approx(0.8)
    assert (await sim.execute(ReturnToDock())).status == "executed"
    assert (await sim.execute(PauseMow())).status == "executed"
    assert (await sim.get_state()).activity is MowerActivity.PAUSED
    assert (await sim.execute(ResumeMow())).reason == "resumed returning"
    _run_until(sim, lambda s: s.activity is MowerActivity.DOCKED)


async def test_emergency_stop_pauses_a_mow() -> None:
    sim = await _connected()
    await sim.execute(StartMow(area_ids=["front"]))
    sim.tick(40)
    assert (await sim.execute(EmergencyStop())).status == "executed"
    state = await sim.get_state()
    assert state.activity is MowerActivity.PAUSED
    assert state.raw["emergency_stopped"] is True
    assert state.raw["blade_on"] is False
    assert (await sim.execute(ResumeMow())).status == "executed"
    assert "emergency_stopped" not in (await sim.get_state()).raw


async def test_manual_drive_session() -> None:
    sim = await _connected()
    session = await sim.start_drive_session()
    assert session.is_open
    assert (await sim.get_state()).activity is MowerActivity.IDLE
    assert (await sim.get_state()).raw["phase"] == "manual_drive"
    await session.update(1.0, 0.0, True)
    assert sim.blade_on
    state = sim.tick(2)
    assert sim.local_position[0] == pytest.approx(0.0)
    assert state.speed_mps == pytest.approx(0.5)
    await session.update(0.0, 1.0, False)
    sim.tick(1)
    assert not sim.blade_on
    await session.stop()
    position = sim.local_position
    sim.tick(5)
    assert sim.local_position == position
    await session.close()
    await session.close()  # idempotent
    assert not session.is_open
    with pytest.raises(CommandRejected, match="closed"):
        await session.update(1, 0, False)


async def test_manual_drive_rules() -> None:
    sim = SimulatedMower(config=SimulationConfig(manual_drive_blades=False), clock=ManualClock())
    await sim.connect()
    caps = await sim.capabilities()
    assert caps.manual_drive and not caps.manual_drive_blades
    session = await sim.start_drive_session()
    await session.update(0.5, 0.0, True)
    assert not sim.blade_on  # forced off by the adapter as well

    await sim.execute(StartMow(area_ids=["front"]))
    assert not session.is_open  # a job ends the session
    with pytest.raises(CommandRejected, match="pause"):
        await sim.start_drive_session()
    await sim.execute(PauseMow())
    session = await sim.start_drive_session()  # cancels the paused job
    assert "job_areas" not in (await sim.get_state()).raw
    sim.lift()
    assert not session.is_open
    sim.clear_error()
    session = await sim.start_drive_session()
    await sim.execute(EmergencyStop())
    assert not session.is_open
    session = await sim.start_drive_session()
    await sim.execute(ReturnToDock())
    assert not session.is_open
    await sim.disconnect()


async def test_virtual_fence_blocks_manual_drive_into_no_go_zone() -> None:
    fenced = MowerMap(
        reference=GeoReference(origin_lat=48, origin_lon=11),
        areas=[MapArea(id="a", boundary=[(-5, -5), (5, -5), (5, 5), (-5, 5)])],
        no_go_zones=[MapZone(id="z", boundary=[(1, -1), (2, -1), (2, 1), (1, 1)])],
        dock=DockPose(x=0, y=0),
    )
    sim = SimulatedMower(mower_map=fenced, clock=ManualClock())
    await sim.connect()
    session = await sim.start_drive_session()
    await session.update(1.0, 0.0, False)
    sim.tick(10)
    assert 0.9 <= sim.local_position[0] < 1.0
    assert not point_in_polygon(sim.local_position, fenced.no_go_zones[0].boundary)


async def test_watch_state_delivers_updates_and_ends_on_disconnect() -> None:
    sim = await _connected()
    stream = sim.watch_state()
    first = await anext(stream)
    assert first.activity is MowerActivity.DOCKED
    await sim.execute(StartMow(area_ids=["front"]))
    sim.tick(10)
    after_start = await anext(stream)
    assert after_start.activity is MowerActivity.MOWING
    ticked = await anext(stream)
    assert ticked.raw["elapsed_s"] == 10
    await sim.disconnect()
    await sim.disconnect()  # idempotent
    with pytest.raises(StopAsyncIteration):
        await anext(stream)
    with pytest.raises(StopAsyncIteration):
        await anext(stream)


async def test_not_connected() -> None:
    sim = _sim()
    assert not sim.is_connected
    assert sim.kind is ConnectionKind.SIMULATED
    with pytest.raises(NotConnected):
        await sim.get_state()
    with pytest.raises(NotConnected):
        sim.watch_state()
    with pytest.raises(NotConnected):
        await sim.get_map()
    with pytest.raises(NotConnected):
        await sim.execute(PauseMow())
    with pytest.raises(NotConnected):
        await sim.start_drive_session()
    assert sim.tick(1).activity is MowerActivity.DOCKED  # ticking works offline
    await sim.connect()
    await sim.connect()
    assert sim.is_connected


async def test_deterministic_with_fixed_clock() -> None:
    async def scenario() -> list[MowerState]:
        sim = SimulatedMower(mower_id=uuid4(), clock=ManualClock())
        sim.mower_id = sim_id
        await sim.connect()
        await sim.execute(StartMow(area_ids=["front", "back"], pattern={"name": "diagonal"}))
        states = [sim.tick(7.3) for _ in range(300)]
        await sim.execute(PauseMow())
        states.append(sim.tick(1))
        return states

    sim_id = uuid4()
    assert await scenario() == await scenario()


async def test_run_ticks_in_real_time_with_injected_sleep() -> None:
    sim = await _connected(time_scale=10)
    sleeps: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)
        if len(sleeps) > 3:
            raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await sim.run(0.5, sleep=fake_sleep)
    assert sleeps == [0.5] * 4
    assert sim.elapsed_s == pytest.approx(15.0)
    with pytest.raises(ValueError, match="interval"):
        await sim.run(0)


def test_invalid_configuration() -> None:
    with pytest.raises(ValueError, match="time_scale"):
        SimulationConfig(time_scale=0)
    with pytest.raises(ValueError, match="battery_percent"):
        SimulationConfig(battery_percent=101)
    with pytest.raises(ValueError, match="dock"):
        SimulatedMower(mower_map=MowerMap(reference=GeoReference(origin_lat=0, origin_lon=0)))
    with pytest.raises(ValueError, match="dt"):
        _sim().tick(-1)


def test_factory_options() -> None:
    mower_id, account_id = uuid4(), uuid4()
    sim = create_simulated_connection(
        ConnectionConfig(mower_id=mower_id, account_id=account_id, options={"time_scale": 5})
    )
    assert sim.mower_id == mower_id
    assert sim.account_id == account_id
    assert sim.config.time_scale == 5
    with pytest.raises(ValueError, match="warp"):
        create_simulated_connection(
            ConnectionConfig(mower_id=mower_id, account_id=account_id, options={"warp": 9})
        )


async def test_capabilities_and_state_metadata() -> None:
    sim = await _connected()
    caps = await sim.capabilities()
    assert caps.native_patterns == frozenset({"stripes", "diagonal", "checkerboard"})
    assert caps.custom_paths
    assert caps.camera == "cloud"
    assert caps.blade_height_range_mm == (20, 100)
    state = await sim.get_state()
    assert state.firmware == "sim-1.0.0"
    assert state.connection is ConnectionKind.SIMULATED
    assert state.mower_id == sim.mower_id
    assert state.observed_at == ManualClock()()


async def test_origin_option_places_the_garden_elsewhere() -> None:
    sim = create_simulated_connection(
        ConnectionConfig(
            mower_id=uuid4(),
            account_id=uuid4(),
            options={"origin_lat": 52.52, "origin_lon": 13.405},
        )
    )
    await sim.connect()
    mower_map = await sim.get_map()
    assert (mower_map.reference.origin_lat, mower_map.reference.origin_lon) == (52.52, 13.405)
    state = await sim.get_state()
    assert state.position is not None
    assert state.position.lat == pytest.approx(52.52, abs=1e-3)
    assert state.position.lon == pytest.approx(13.405, abs=1e-3)
    assert _sim().config.origin == (48.137154, 11.576124)


@pytest.mark.parametrize(
    ("options", "message"),
    [
        ({"origin_lat": 52.5}, "go together"),
        ({"origin_lat": 91.0, "origin_lon": 0.0}, "origin_lat"),
        ({"origin_lat": 0.0, "origin_lon": 181.0}, "origin_lon"),
    ],
)
def test_origin_option_is_validated(options: dict[str, float], message: str) -> None:
    with pytest.raises(ValueError, match=message):
        SimulationConfig(origin_lat=options.get("origin_lat"), origin_lon=options.get("origin_lon"))


def _spot_plan() -> SpotMowPlan:
    return SpotMowPlan.build(
        [
            SpotSegment.from_points([(2, 2), (6, 2)], blades_on=True, speed_mps=0.5),
            SpotSegment.from_points(
                [(6, 2), (6, 2.35)], blades_on=True, speed_mps=0.5, kind="turn"
            ),
            SpotSegment.from_points([(6, 2.35), (2, 2.35)], blades_on=True, speed_mps=0.5),
            SpotSegment.from_points(
                [(2, 2.35), (2, 5)], blades_on=False, speed_mps=0.8, kind="transfer"
            ),
            SpotSegment.from_points([(2, 5), (5, 5)], blades_on=True, speed_mps=3.0 / 2),
        ],
        ["stripes", "remow"],
    )


async def test_simulator_follows_a_spot_plan() -> None:
    sim = await _connected()
    plan = _spot_plan()
    outcome = await sim.execute(StartMow(spot_plan=plan))
    assert outcome.status.value == "executed"
    assert plan.plan_hash[:12] in (outcome.reason or "")
    state = sim.snapshot()
    assert state.raw["job_pattern"] == "stripes+remow"
    assert state.raw["job_plan_hash"] == plan.plan_hash
    assert state.raw["job_areas"] == ["spot"]
    seen: list[tuple[float, float, bool]] = []
    progress: list[int] = []

    def record(s: MowerState) -> bool:
        seen.append((s.raw["x"], s.raw["y"], s.raw["blade_on"]))
        progress.append(s.raw.get("progress_percent", -1))
        return s.activity is MowerActivity.RETURNING

    _run_until(sim, record, dt=0.25)
    on_plan = [(x, y) for x, y, blade in seen if blade]
    assert on_plan, "the blades never ran"
    for x, y in on_plan:
        assert 1.99 <= x <= 6.01
        assert 1.99 <= y <= 5.01
    transfer = [blade for x, y, blade in seen if abs(x - 2.0) < 1e-6 and 2.5 < y < 4.9]
    assert transfer
    assert not any(transfer)
    assert max(progress) >= 99


async def test_simulator_without_custom_paths_refuses_spot_plans() -> None:
    sim = await _connected(custom_paths=False)
    assert not (await sim.capabilities()).custom_paths
    outcome = await sim.execute(StartMow(spot_plan=_spot_plan()))
    assert outcome.reason == "custom paths not supported"


async def test_simulator_spot_plan_needs_battery() -> None:
    sim = await _connected(battery_percent=10)
    outcome = await sim.execute(StartMow(spot_plan=_spot_plan()))
    assert outcome.reason == "battery too low to start"


async def test_delayed_start_is_sent_then_confirmed_by_the_state() -> None:
    sim = await _connected(start_delay_s=5, confirm_timeout_s=5)
    dispatch = await sim.dispatch(StartMow(area_ids=["front"]))
    assert dispatch.confirm is not None
    assert dispatch.sent == "start_mow sent"
    assert sim.snapshot().activity is MowerActivity.DOCKED  # not started yet
    waiting = asyncio.create_task(dispatch.settle())
    await asyncio.sleep(0)
    sim.tick(4)
    await asyncio.sleep(0)
    assert not waiting.done()
    sim.tick(1)
    outcome = await asyncio.wait_for(waiting, 1)
    assert outcome.reason == "mower is mowing"
    assert sim.snapshot().activity is MowerActivity.MOWING


async def test_delayed_start_without_confirmation_is_accepted() -> None:
    sim = await _connected(start_delay_s=float("inf"), confirm_timeout_s=0.05)
    outcome = await sim.execute(StartMow(area_ids=["front"]))
    assert str(outcome.status) == "accepted"
    assert "did not report mowing within 0.05 s" in (outcome.reason or "")
    sim.tick(3600)
    assert sim.snapshot().activity is not MowerActivity.MOWING


@pytest.mark.parametrize("command", [PauseMow(), ReturnToDock(), EmergencyStop()])
async def test_pause_return_or_stop_cancel_a_pending_start(
    command: PauseMow | ReturnToDock | EmergencyStop,
) -> None:
    sim = await _connected(start_delay_s=5)
    await sim.dispatch(StartMow(area_ids=["front"]))
    assert str((await sim.execute(command)).status) == "executed"
    sim.tick(10)
    assert sim.snapshot().activity is not MowerActivity.MOWING


async def test_delayed_resume_waits_for_mowing() -> None:
    sim = await _connected(start_delay_s=2, confirm_timeout_s=5)
    sim.config = SimulationConfig()  # start at once
    await sim.execute(StartMow(area_ids=["front"]))
    await sim.execute(PauseMow())
    sim.config = SimulationConfig(start_delay_s=2, confirm_timeout_s=5)
    dispatch = await sim.dispatch(ResumeMow())
    assert dispatch.confirm is not None
    assert sim.snapshot().activity is MowerActivity.PAUSED
    sim.tick(2)
    assert str((await dispatch.settle()).status) == "executed"


@pytest.mark.parametrize(
    ("options", "message"),
    [({"start_delay_s": -1.0}, "start_delay_s"), ({"confirm_timeout_s": 0.0}, "confirm_timeout_s")],
)
def test_start_delay_options_are_validated(options: dict[str, Any], message: str) -> None:
    with pytest.raises(ValueError, match=message):
        SimulationConfig(**options)


@pytest.mark.parametrize("end_job", [False, True])
async def test_return_to_dock_leaves_no_job_to_resume(end_job: bool) -> None:
    sim = await _connected()
    assert (await sim.execute(StartMow(area_ids=["front"]))).status == "executed"
    sim.tick(30)
    assert (await sim.execute(PauseMow())).status == "executed"
    outcome = await sim.execute(ReturnToDock(end_job=end_job))
    assert outcome.status == "executed"
    assert ("job ended" in (outcome.reason or "")) is end_job
    state = await sim.get_state()
    assert state.activity is MowerActivity.RETURNING
    assert "job_areas" not in state.raw
    assert (await sim.execute(ResumeMow())).status == "rejected"
