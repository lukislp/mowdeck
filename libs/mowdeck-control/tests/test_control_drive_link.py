"""The drive link of spot runs and drive calibrations, against the simulator only: a link that
reports the mower's signal below ``LINK_MIN_RSSI_DBM`` refuses the start, and a drive link that
does not come up or drops during a run ends it ``link_lost`` (never ``frame_rejected``). Real
refusals keep ``frame_rejected``. No manufacturer package is imported."""

from datetime import UTC, datetime

from mowdeck_contracts.v1 import (
    LINK_BLE_BRIDGE,
    LINK_MIN_RSSI_DBM,
    CommandStatus,
    DriveCalibrate,
    LinkStatus,
    ManualDrive,
    MowerState,
    SpotMow,
)
from mowdeck_control import (
    CommandRejected,
    NotConnected,
    SimulationConfig,
    calibration_start_problem,
    spot_start_problem,
    weak_link_problem,
)
from mowdeck_control.spot_run import REASON_FRAME_REJECTED, REASON_LINK_LOST, refusal_reason
from test_control_spot_run import Harness, plan_of, state_at

NOW = datetime(2026, 6, 1, 12, 0, 0, tzinfo=UTC)
BLE_NOT_UP = "the Bluetooth connection through the bridge did not come up"
"""How an adapter words a drive link that did not attach (a weak signal at the bridge)."""


def bridge_state(rssi: int | None, *, connected: bool = True) -> MowerState:
    state = state_at(3.0, 6.0, 0.0, NOW)
    return state.model_copy(
        update={"links": {LINK_BLE_BRIDGE: LinkStatus(connected=connected, rssi=rssi)}}
    )


def bridge_config(**changes: object) -> SimulationConfig:
    values: dict[str, object] = {"state_period_s": 1.0, "bridge": True}
    values.update(changes)
    return SimulationConfig(**values)  # type: ignore[arg-type]


# --- the signal as a start condition ------------------------------------------------------------


def test_control_weak_link_threshold_and_unknown_signal() -> None:
    assert LINK_MIN_RSSI_DBM == -90
    weak = weak_link_problem(bridge_state(-97))
    assert weak is not None and "-97 dBm" in weak and "-90 dBm" in weak
    assert LINK_BLE_BRIDGE in weak
    assert weak_link_problem(bridge_state(-91)) is not None
    assert weak_link_problem(bridge_state(-90)) is None  # the threshold itself is enough
    assert weak_link_problem(bridge_state(-74)) is None
    assert weak_link_problem(bridge_state(None)) is None  # unknown: not blocking
    assert weak_link_problem(state_at(3.0, 6.0, 0.0, NOW)) is None  # no additional link


def test_control_start_checks_refuse_a_weak_link() -> None:
    assert spot_start_problem(bridge_state(-97), now=NOW) == weak_link_problem(bridge_state(-97))
    assert spot_start_problem(bridge_state(-74), now=NOW) is None
    assert spot_start_problem(bridge_state(None), now=NOW) is None
    weak = calibration_start_problem(bridge_state(-97), now=NOW)
    assert weak is not None and "-97 dBm" in weak
    assert calibration_start_problem(bridge_state(-74), now=NOW) is None
    assert calibration_start_problem(bridge_state(None), now=NOW) is None


async def test_control_spot_run_and_calibration_refused_on_a_weak_bridge_signal() -> None:
    h = Harness(config=bridge_config(bridge_rssi=-97))
    await h.start()
    spot = await h.send(SpotMow(plan=plan_of([(3.0, 6.0), (6.0, 6.0)]), return_to_dock=False))
    assert spot.status is CommandStatus.REJECTED
    assert "-97 dBm" in (spot.reason or "") and not h.controller.spot_run_active
    calibration = await h.send(DriveCalibrate())
    assert calibration.status is CommandStatus.REJECTED
    assert "-97 dBm" in (calibration.reason or "") and not h.controller.calibration_active
    await h.close()

    good = Harness(config=bridge_config(bridge_rssi=-74))
    await good.start()
    started = await good.send(SpotMow(plan=plan_of([(3.0, 6.0), (6.0, 6.0)])))
    assert started.status is CommandStatus.ACCEPTED, started.reason
    await good.close()


# --- a drive link that does not come up or drops ------------------------------------------------


async def test_control_spot_run_ends_link_lost_when_the_bridge_drops() -> None:
    h = Harness(config=bridge_config())
    await h.start()
    started = await h.send(SpotMow(plan=plan_of([(3.0, 6.0), (9.0, 6.0)])))
    assert started.status is CommandStatus.ACCEPTED, started.reason
    await h.until(lambda: h.controller.drive_session_active, 10, "no drive session")
    h.sim.set_bridge(False)
    report = await h.controller.spot_run_result(started.command_id)
    assert report is not None
    assert (report.state, report.reason) == ("aborted", REASON_LINK_LOST)
    assert not h.controller.drive_session_active
    await h.close()


async def test_control_calibration_ends_link_lost_when_the_bridge_drops() -> None:
    h = Harness(config=bridge_config())
    await h.start()
    started = await h.send(DriveCalibrate())
    assert started.status is CommandStatus.ACCEPTED, started.reason
    await h.until(lambda: h.controller.drive_session_active, 10, "no drive session")
    h.sim.set_bridge(False)
    run = await h.controller.calibration_result(started.command_id)
    assert run is not None and (run.state, run.reason) == ("aborted", REASON_LINK_LOST)
    await h.close()


async def test_control_drive_link_that_does_not_come_up_ends_runs_link_lost() -> None:
    """The live case: the bridge holds its own link, but the Bluetooth link to the mower does
    not attach (the adapter raises ``NotConnected``). A joystick frame fails with the adapter's
    words, a spot run and a calibration end ``link_lost``."""
    h = Harness(config=bridge_config())
    await h.start()

    async def not_up() -> None:
        raise NotConnected(BLE_NOT_UP)

    h.sim.start_drive_session = not_up  # type: ignore[assignment,method-assign]
    frame = await h.send(ManualDrive(linear=0.3, angular=0.0))
    assert (frame.status, frame.reason) == (CommandStatus.FAILED, BLE_NOT_UP)
    assert not h.controller.drive_session_active

    spot = await h.send(SpotMow(plan=plan_of([(3.0, 6.0), (6.0, 6.0)]), return_to_dock=False))
    assert spot.status is CommandStatus.ACCEPTED, spot.reason
    report = await h.controller.spot_run_result(spot.command_id)
    assert report is not None
    assert (report.state, report.reason) == ("aborted", REASON_LINK_LOST)
    assert BLE_NOT_UP in (report.detail or "")

    calibration = await h.send(DriveCalibrate())
    assert calibration.status is CommandStatus.ACCEPTED, calibration.reason
    run = await h.controller.calibration_result(calibration.command_id)
    assert run is not None and (run.state, run.reason) == ("aborted", REASON_LINK_LOST)
    await h.close()


def test_control_real_refusals_keep_frame_rejected() -> None:
    assert refusal_reason("rejected: speed out of range") == REASON_FRAME_REJECTED
    assert refusal_reason("failed: " + BLE_NOT_UP) == REASON_LINK_LOST
    link_down = "failed: drive link down (manual driving is not supported)"
    assert refusal_reason(link_down) == REASON_LINK_LOST


async def test_control_spot_run_refused_frame_with_the_link_up_stays_frame_rejected() -> None:
    h = Harness(config=bridge_config())
    await h.start()

    async def refuse(linear: float, angular: float, blade_on: bool) -> None:
        raise CommandRejected("the mower refused the frame")

    started = await h.send(SpotMow(plan=plan_of([(3.0, 6.0), (9.0, 6.0)])))
    assert started.status is CommandStatus.ACCEPTED, started.reason
    await h.until(lambda: h.controller.drive_session_active, 10, "no drive session")
    session = h.controller._session
    assert session is not None
    session.update = refuse  # type: ignore[method-assign]
    report = await h.controller.spot_run_result(started.command_id)
    assert report is not None
    assert (report.state, report.reason) == ("aborted", REASON_FRAME_REJECTED)
    await h.close()


async def test_control_spot_run_refused_with_the_link_reported_down_is_link_lost() -> None:
    """An adapter that words the missing link as a refusal: the state reports the bridge link
    down, so the run still ends ``link_lost``."""
    h = Harness(config=bridge_config())
    await h.start()

    async def refused() -> None:
        raise CommandRejected("the Bluetooth bridge is not connected")

    started = await h.send(SpotMow(plan=plan_of([(3.0, 6.0), (9.0, 6.0)])))
    assert started.status is CommandStatus.ACCEPTED, started.reason
    await h.until(lambda: h.controller.drive_session_active, 10, "no drive session")
    h.sim.start_drive_session = refused  # type: ignore[assignment,method-assign]
    h.sim.set_bridge(False)
    report = await h.controller.spot_run_result(started.command_id)
    assert report is not None
    assert (report.state, report.reason) == ("aborted", REASON_LINK_LOST)
    assert "drive link down" in (report.detail or "")
    await h.close()
