"""Drive calibration (ADR 0042) against the simulator only: the fit recovers the configured
full-scale speed and yaw rate, the forward budget, the start conditions, the abort paths, rule 13
while it runs and the drive speed setting. No manufacturer package is imported."""

import math

import pytest
from mowdeck_contracts.v1 import (
    CommandStatus,
    DriveCalibrate,
    DriveCalibration,
    EmergencyStop,
    ManualDrive,
    RtkFixStatus,
    SetDriveSpeed,
    SetLight,
    SpotStop,
    StartMow,
)
from mowdeck_control import CalibrationConfig, SimulationConfig, SpotRunConfig
from mowdeck_control.calibration import REASON_TURN_DIRECTION
from mowdeck_control.controller import CALIBRATION_ACTIVE
from mowdeck_control.spot_run import REASON_EMERGENCY_STOP, REASON_STOPPED_BY_USER
from test_control_spot_run import Harness


async def calibrate(h: Harness, step_s: float = 2.5) -> tuple[str, DriveCalibration | None]:
    started = await h.send(DriveCalibrate(step_s=step_s))
    assert started.status is CommandStatus.ACCEPTED, started.reason
    run = await h.controller.calibration_result(started.command_id)
    assert run is not None
    await h.settle()
    return run.state, run.result.calibration if run.result else None


@pytest.mark.parametrize(
    ("speed", "yaw_dps"),
    [(0.5, 57.3), (0.1, 3.0), (0.3, 20.0)],
)
async def test_control_calibration_recovers_the_simulator_within_10_percent(
    speed: float, yaw_dps: float
) -> None:
    config = SimulationConfig(
        state_period_s=1.0, drive_max_speed_mps=speed, drive_max_turn_rad_s=math.radians(yaw_dps)
    )
    h = Harness(config=config)
    await h.start()
    state, calibration = await calibrate(h)
    assert state == "done" and calibration is not None
    assert calibration.full_speed_mps == pytest.approx(speed, rel=0.1)
    assert calibration.full_yaw_rate_dps == pytest.approx(yaw_dps, rel=0.1)
    assert calibration.dead_zone < 0.1 and calibration.turn_dead_zone < 0.1
    assert calibration.drive_speed_setting == pytest.approx(0.5)  # the simulator's setting
    status = h.controller.calibration_status
    assert status is not None and status.final and status.result is not None
    assert len(status.steps) == 10
    measured = [s for s in status.steps if not s.skipped]
    assert all(s.reports >= 1 for s in measured)
    assert all(s.report_interval_s in (None, pytest.approx(1.0, abs=0.05)) for s in measured)
    assert status.result.forward_m <= 1.5 + 0.5 * speed + 0.05
    final = h.controller.last_state
    assert final is not None and final.drive_calibration == status
    assert not h.controller.drive_session_active and not h.controller.calibration_active
    assert not h.blade_seen
    await h.close()


async def test_control_calibration_keeps_the_forward_budget() -> None:
    h = Harness(config=SimulationConfig(state_period_s=1.0, drive_max_speed_mps=0.8))
    await h.start()
    start = h.sim.local_position
    state, calibration = await calibrate(h)
    assert state == "done" and calibration is not None
    assert calibration.full_speed_mps == pytest.approx(0.8, rel=0.1)
    status = h.controller.calibration_status
    assert status is not None
    assert any(s.skipped for s in status.steps if s.kind == "forward")
    assert math.dist(start, h.sim.local_position) < 1.5 + 0.8 + 0.05
    await h.close()


async def test_control_calibration_detects_a_dead_zone() -> None:
    """A mower that only moves above 20 % of the stick: the fit finds the zone."""
    h = Harness(config=SimulationConfig(state_period_s=1.0))
    await h.start()
    original = h.sim._drive_step

    def with_dead_zone(t: float) -> float:
        cmd = h.sim._drive_cmd
        linear = 0.0 if abs(cmd.linear) <= 0.2 else (abs(cmd.linear) - 0.2) / 0.8
        h.sim._drive_cmd = type(cmd)(
            linear=math.copysign(linear, cmd.linear), angular=cmd.angular, blade_on=cmd.blade_on
        )
        try:
            return original(t)
        finally:
            h.sim._drive_cmd = cmd

    h.sim._drive_step = with_dead_zone  # type: ignore[method-assign]
    state, calibration = await calibrate(h)
    assert state == "done" and calibration is not None
    assert calibration.dead_zone == pytest.approx(0.2, abs=0.05)
    assert calibration.full_speed_mps == pytest.approx(0.5, rel=0.1)
    tuned = SpotRunConfig().calibrated(calibration)
    assert tuned.min_linear == pytest.approx(calibration.dead_zone + 0.05)
    assert tuned.speed_at(tuned.stick_for_speed(0.2)) == pytest.approx(0.2)
    await h.close()


async def test_control_calibration_fails_on_an_inverted_turn_direction() -> None:
    h = Harness(config=SimulationConfig(state_period_s=1.0, drive_max_turn_rad_s=-1.0))
    await h.start()
    state, calibration = await calibrate(h)
    assert state == "failed" and calibration is None
    run = h.controller._calibration
    assert run is not None and run.reason == REASON_TURN_DIRECTION
    await h.close()


async def test_control_calibration_start_conditions() -> None:
    h = Harness(config=SimulationConfig(state_period_s=1.0))
    await h.start(idle=False)  # docked
    docked = await h.send(DriveCalibrate())
    assert docked.status is CommandStatus.REJECTED and "idle" in (docked.reason or "")
    await h.send(ManualDrive(linear=0, angular=0))
    await h.controller.end_drive_session()
    await h.settle()
    h.sim.set_rtk(RtkFixStatus.FLOAT)
    await h.settle()
    floating = await h.send(DriveCalibrate())
    assert floating.status is CommandStatus.REJECTED and "RTK" in (floating.reason or "")
    h.sim.set_rtk(RtkFixStatus.FIXED)
    # turn towards the lawn's near edge: no room ahead
    h.sim._theta = math.pi
    h.sim._x = 1.5
    h.sim._publish()
    await h.settle()
    cramped = await h.send(DriveCalibrate())
    assert cramped.status is CommandStatus.REJECTED
    assert "room ahead" in (cramped.reason or "")
    await h.close()


async def test_control_calibration_rule_13_and_stops() -> None:
    h = Harness(config=SimulationConfig(state_period_s=1.0))
    await h.start()
    started = await h.send(DriveCalibrate())
    assert started.status is CommandStatus.ACCEPTED
    await h.until(lambda: h.controller.drive_session_active, 5, "no drive session")
    assert h.controller.calibration_active
    blocked = await h.send(StartMow(area_ids=["lawn"]))
    assert (blocked.status, blocked.reason) == (CommandStatus.REJECTED, CALIBRATION_ACTIVE)
    frame = await h.send(ManualDrive(linear=0.5, angular=0))
    assert (frame.status, frame.reason) == (CommandStatus.REJECTED, CALIBRATION_ACTIVE)
    again = await h.send(DriveCalibrate())
    assert again.status is CommandStatus.REJECTED
    light = await h.send(SetLight(on=True))
    assert light.status is not CommandStatus.REJECTED or light.reason != CALIBRATION_ACTIVE
    stopped = await h.send(SpotStop())
    assert stopped.status is CommandStatus.EXECUTED
    run = await h.controller.calibration_result(started.command_id)
    assert run is not None and (run.state, run.reason) == ("aborted", REASON_STOPPED_BY_USER)
    assert not h.controller.drive_session_active
    # a second calibration ends with the emergency stop
    second = await h.send(DriveCalibrate())
    assert second.status is CommandStatus.ACCEPTED, second.reason
    await h.until(lambda: h.controller.drive_session_active, 5, "no drive session")
    await h.send(EmergencyStop())
    run = await h.controller.calibration_result(second.command_id)
    assert run is not None and (run.state, run.reason) == ("aborted", REASON_EMERGENCY_STOP)
    await h.close()


async def test_control_calibration_aborts_on_a_lost_fix() -> None:
    h = Harness(config=SimulationConfig(state_period_s=1.0))
    await h.start()
    started = await h.send(DriveCalibrate())
    await h.until(lambda: h.controller.drive_session_active, 5, "no drive session")
    h.sim.set_rtk(RtkFixStatus.FLOAT)
    run = await h.controller.calibration_result(started.command_id)
    assert run is not None and (run.state, run.reason) == ("aborted", "rtk_lost")
    await h.close()


async def test_control_drive_speed_setting_rescales_the_calibration() -> None:
    h = Harness(config=SimulationConfig(state_period_s=1.0, drive_max_speed_mps=0.2))
    await h.start()
    caps = await h.controller.capabilities()
    assert caps.drive_speed_setting is not None
    state, calibration = await calibrate(h, step_s=2.0)
    assert state == "done" and calibration is not None
    assert calibration.full_speed_mps == pytest.approx(0.2, rel=0.1)
    changed = await h.send(SetDriveSpeed(speed_mps=1.04))
    assert changed.status is CommandStatus.EXECUTED and "1 m/s" in (changed.reason or "")
    await h.settle()
    state_now = h.controller.last_state
    assert state_now is not None and state_now.drive_speed_mps == pytest.approx(1.0)
    h.calibration = calibration
    tuned = h.controller.spot_config(state_now)
    assert tuned.assumed_full_speed_mps == pytest.approx(calibration.full_speed_mps * 2)
    assert h.sim.drive_full_speed_mps == pytest.approx(0.4)
    await h.close()


async def test_control_drive_speed_setting_needs_the_capability() -> None:
    h = Harness(config=SimulationConfig(state_period_s=1.0, drive_speed_setting=False))
    await h.start()
    refused = await h.send(SetDriveSpeed(speed_mps=0.4))
    assert refused.status is CommandStatus.REJECTED
    state = h.controller.last_state
    assert state is not None and state.drive_speed_mps is None
    await h.close()


def test_control_calibration_config_defaults() -> None:
    config = CalibrationConfig()
    assert config.forward_commands == (0.3, 0.5, 0.7, 1.0)
    assert sorted(abs(c) for c in config.turn_commands) == [0.3, 0.3, 0.6, 0.6, 1.0, 1.0]
    assert config.step_s == 2.5 and config.max_forward_m == 1.5
