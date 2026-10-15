"""Drive calibration (ADR 0042) against the simulator only: the fit recovers the configured
full-scale speed and yaw rate, the forward budget, the start conditions, the abort paths, rule 13
while it runs and the drive speed setting. No manufacturer package is imported."""

import math
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from mowdeck_contracts.v1 import (
    CommandStatus,
    DriveCalibrate,
    DriveCalibration,
    DriveCalibrationStep,
    EmergencyStop,
    ManualDrive,
    RtkFixStatus,
    SetDriveSpeed,
    SetLight,
    SpotStop,
    StartMow,
)
from mowdeck_control import CalibrationConfig, SimulationConfig, SpotRunConfig
from mowdeck_control.calibration import (
    NOTE_TOO_FEW_REPORTS,
    REASON_NO_FIT,
    REASON_TURN_DIRECTION,
    DriveCalibrationRun,
    _measure,
    _Report,
)
from mowdeck_control.controller import CALIBRATION_ACTIVE
from mowdeck_control.spot_run import REASON_EMERGENCY_STOP, REASON_STOPPED_BY_USER
from test_control_spot_run import MAP, Harness


async def calibrate(h: Harness, step_s: float = 6.0) -> tuple[str, DriveCalibration | None]:
    started = await h.send(DriveCalibrate(step_s=step_s))
    assert started.status is CommandStatus.ACCEPTED, started.reason
    run = await h.controller.calibration_result(started.command_id)
    assert run is not None
    await h.settle()
    return run.state, run.result.calibration if run.result else None


def hardware_like(**changes: float) -> SimulationConfig:
    """A simulated mower like the first hardware run: 1 s between reports, wheels 1.5 s late."""
    values: dict[str, float] = {"state_period_s": 1.0, "drive_response_delay_s": 1.5}
    values.update(changes)
    return SimulationConfig(**values)  # type: ignore[arg-type]


def patch_turns(h: Harness, scale: Callable[[float], float]) -> None:
    """Make the simulated mower turn ``scale(angular)`` instead of ``angular``."""
    original = h.sim._drive_step

    def scaled(t: float) -> float:
        cmd = h.sim._drive_cmd
        h.sim._drive_cmd = type(cmd)(
            linear=cmd.linear, angular=scale(cmd.angular), blade_on=cmd.blade_on
        )
        try:
            return original(t)
        finally:
            h.sim._drive_cmd = cmd

    h.sim._drive_step = scaled  # type: ignore[method-assign]


@pytest.mark.parametrize(
    ("speed", "yaw_dps"),
    [(0.1, 3.0), (0.3, 20.0), (0.25, 57.3)],
)
async def test_control_calibration_recovers_a_late_mower_within_15_percent(
    speed: float, yaw_dps: float
) -> None:
    """Reports every second and a mower that follows the stick 1.5 s late: the steady windows
    still give the configured speed and yaw rate."""
    h = Harness(
        config=hardware_like(drive_max_speed_mps=speed, drive_max_turn_rad_s=math.radians(yaw_dps))
    )
    await h.start()
    start = h.sim.local_position
    state, calibration = await calibrate(h)
    assert state == "done" and calibration is not None
    assert calibration.full_speed_mps == pytest.approx(speed, rel=0.15)
    assert calibration.full_yaw_rate_dps == pytest.approx(yaw_dps, rel=0.15)
    assert calibration.dead_zone < 0.1 and calibration.turn_dead_zone < 0.1
    assert calibration.drive_speed_setting == pytest.approx(0.5)  # the simulator's setting
    status = h.controller.calibration_status
    assert status is not None and status.final and status.result is not None
    assert len(status.steps) == 10
    measured = [s for s in status.steps if s.outcome == "measured"]
    assert sum(s.kind == "forward" for s in measured) >= 2
    assert sum(s.kind == "turn" for s in measured) == 6
    assert all(s.reports >= 4 and (s.window_s or 0) >= 3.0 for s in measured)
    assert all(s.report_interval_s == pytest.approx(1.0, abs=0.05) for s in measured)
    assert all(s.outcome == "skipped" for s in status.steps if s not in measured)
    assert status.result.forward_m <= 1.5 + 0.05
    assert math.dist(start, h.sim.local_position) <= 1.5 + 0.05
    final = h.controller.last_state
    assert final is not None and final.drive_calibration == status
    assert not h.controller.drive_session_active and not h.controller.calibration_active
    assert not h.blade_seen
    await h.close()


async def test_control_calibration_keeps_the_forward_budget() -> None:
    """A fast mower: the later forward steps are skipped to keep the 1.5 m, and with a single
    forward step left the calibration fails with a reason instead of guessing."""
    h = Harness(config=hardware_like(drive_max_speed_mps=0.8))
    await h.start()
    start = h.sim.local_position
    state, calibration = await calibrate(h)
    assert state == "failed" and calibration is None
    status = h.controller.calibration_status
    assert status is not None
    forward = [s for s in status.steps if s.kind == "forward"]
    assert forward[0].outcome == "measured"
    assert forward[0].measured == pytest.approx(0.24, rel=0.05)
    assert all(s.outcome == "skipped" and s.skipped for s in forward[1:])
    assert math.dist(start, h.sim.local_position) < 1.5 + 0.05
    run = h.controller._calibration
    assert run is not None and run.reason == REASON_NO_FIT
    assert "forward" in (run.detail or "") and "3 skipped" in (run.detail or "")
    await h.close()


def test_control_calibration_step_with_two_reports_is_invalid() -> None:
    at = datetime(2026, 10, 15, 12, tzinfo=UTC)
    reports = [_Report(at, 0.0, 0.0, 0.0), _Report(at + timedelta(seconds=1), 0.1, 0.0, 0.0)]
    step = _measure(
        "forward", 0.5, duration_s=6.0, window_s=4.5, reports=reports, config=CalibrationConfig()
    )
    assert step.outcome == "invalid" and step.measured is None and step.reports == 2
    assert (step.note or "").startswith(NOTE_TOO_FEW_REPORTS)
    four = [_Report(at + timedelta(seconds=i), 0.1 * i, 0.0, 0.0) for i in range(4)]
    short = _measure(
        "forward", 0.5, duration_s=4.0, window_s=2.5, reports=four, config=CalibrationConfig()
    )
    assert short.outcome == "invalid", "a window under 3 s is invalid however many reports"
    valid = _measure(
        "forward", 0.5, duration_s=6.0, window_s=4.5, reports=four, config=CalibrationConfig()
    )
    assert valid.outcome == "measured" and valid.measured == pytest.approx(0.1)


async def test_control_calibration_too_few_reports_never_count_as_zero() -> None:
    """Reports every 2.5 s leave at most two in each steady window: every step is invalid (no
    value, not 0) and the calibration fails instead of fitting."""
    h = Harness(config=hardware_like(state_period_s=2.5))
    await h.start()
    state, calibration = await calibrate(h)
    assert state == "failed" and calibration is None
    status = h.controller.calibration_status
    assert status is not None
    driven = [s for s in status.steps if not s.skipped]
    assert driven and all(s.outcome == "invalid" and s.measured is None for s in driven)
    assert all((s.note or "").startswith(NOTE_TOO_FEW_REPORTS) for s in driven)
    run = h.controller._calibration
    assert run is not None and run.reason == REASON_NO_FIT
    await h.close()


async def test_control_calibration_drops_an_inconsistent_turn_pair() -> None:
    """The right turn at 0.3 is three times faster than the left one: both are marked
    inconsistent and the yaw rate is fitted from the other turns."""
    h = Harness(config=hardware_like(drive_max_speed_mps=0.2))
    await h.start()
    patch_turns(h, lambda a: a * 3 if math.isclose(a, -0.3) else a)
    state, calibration = await calibrate(h)
    assert state == "done" and calibration is not None
    assert calibration.full_yaw_rate_dps == pytest.approx(math.degrees(1.0), rel=0.15)
    status = h.controller.calibration_status
    assert status is not None
    by_command = {s.command: s for s in status.steps if s.kind == "turn"}
    assert by_command[0.3].outcome == by_command[-0.3].outcome == "inconsistent"
    assert by_command[-0.3].measured is not None  # the value stays visible
    assert all(by_command[c].outcome == "measured" for c in (0.6, -0.6, 1.0, -1.0))
    await h.close()


async def test_control_calibration_drops_an_outlier() -> None:
    """Full turns slower than the 0.6 turns break the growth with the stick: dropped."""
    h = Harness(config=hardware_like(drive_max_speed_mps=0.2))
    await h.start()
    patch_turns(h, lambda a: a * 0.3 if math.isclose(abs(a), 1.0) else a)
    state, calibration = await calibrate(h)
    assert state == "done" and calibration is not None
    assert calibration.full_yaw_rate_dps == pytest.approx(math.degrees(1.0), rel=0.15)
    status = h.controller.calibration_status
    assert status is not None
    outliers = {s.command for s in status.steps if s.outcome == "outlier"}
    assert outliers == {1.0, -1.0}
    await h.close()


def test_control_calibration_first_hardware_run_gives_no_fit() -> None:
    """The numbers of the first hardware run, as if each step had enough reports: the turn pair
    at 0.3 disagrees, forward 0.3 is an outlier, and one moving forward step is not enough."""
    run = DriveCalibrationRun(
        run_id=uuid4(),
        mower_map=MAP,
        pose_source=lambda: None,
        frames=None,  # type: ignore[arg-type]
    )
    values = [
        ("forward", 0.3, 0.008),
        ("forward", 0.5, 0.0),
        ("forward", 0.7, 0.0),
        ("forward", 1.0, 0.224),
        ("turn", 0.3, 2.0),
        ("turn", -0.3, -5.2),
        ("turn", 0.6, 2.0),
        ("turn", -0.6, -3.0),
        ("turn", 1.0, 0.0),
        ("turn", -1.0, 0.0),
    ]
    run._steps = [
        DriveCalibrationStep(kind=k, command=c, duration_s=6.0, measured=v, reports=5)  # type: ignore[arg-type]
        for k, c, v in values
    ]
    run._fit(0.3)
    assert (run.state, run.reason) == ("failed", REASON_NO_FIT)
    assert run.result is None
    outcomes = {(s.kind, s.command): s.outcome for s in run.status.steps}
    assert outcomes[("turn", 0.3)] == outcomes[("turn", -0.3)] == "inconsistent"
    assert outcomes[("forward", 0.3)] == "outlier"
    assert "forward" in (run.detail or "")


async def test_control_calibration_asks_for_faster_reports() -> None:
    """A connection that polls its reports is asked for the calibration's interval while it
    runs and restored afterwards."""
    h = Harness(config=SimulationConfig(state_period_s=1.0))
    await h.start()
    calls: list[float | None] = []
    h.sim.set_drive_report_interval = calls.append  # type: ignore[attr-defined]
    started = await h.send(DriveCalibrate())
    assert started.status is CommandStatus.ACCEPTED
    await h.until(lambda: h.controller.drive_session_active, 5, "no drive session")
    await h.send(SpotStop())
    await h.controller.calibration_result(started.command_id)
    await h.settle()
    assert calls == [CalibrationConfig().report_interval_s, None]
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
    state, calibration = await calibrate(h)
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
    assert config.step_s == 6.0 and config.max_forward_m == 1.5
    assert (config.settle_s, config.pause_s) == (1.5, 2.0)
    assert (config.min_reports, config.min_window_s) == (4, 3.0)
    assert config.min_step_s == 4.5
