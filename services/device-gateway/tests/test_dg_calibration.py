"""Drive calibration through the gateway (ADR 0042) against the simulator only: ``accepted``
at once and the final result at its end, ``MowerState.drive_calibration`` in the telemetry,
rule 12/13 while it runs, the stored calibration taken off the options before the connection is
created and handed to spot runs, and no reconnect when only the calibration changes. No
manufacturer package is imported."""

import functools
from datetime import UTC, datetime

from dg_support import TestProvider, envelope, make_app, registration, results, telemetry
from mowdeck_contracts.v1 import (
    MOWER_UPDATED,
    CommandStatus,
    DriveCalibrate,
    DriveCalibration,
    EventEnvelope,
    MowerActivity,
    MowerRegistration,
    SpotStop,
    StartMow,
)
from mowdeck_control import MowerConnection, SimulatedMower, SimulationConfig
from mowdeck_control.controller import CALIBRATION_ACTIVE
from mowdeck_service_kit.bus import InMemoryBus, publish_command, publish_event
from mowdeck_service_kit.testing import lifespan_client
from test_dg_spot import LAWN, _leave_the_dock, _results_of, wait_until

from device_gateway.domain.rules import drive_calibration_of, same_link, without_calibration

CALIBRATION = DriveCalibration(
    full_speed_mps=0.12,
    full_yaw_rate_dps=3.5,
    dead_zone=0.15,
    turn_dead_zone=0.2,
    drive_speed_setting=0.5,
    calibrated_at=datetime(2026, 10, 15, 12, 0, tzinfo=UTC),
)


def _app(bus: InMemoryBus, reg: MowerRegistration, seen: list[MowerRegistration]) -> object:
    def factory(registration: MowerRegistration) -> MowerConnection:
        seen.append(registration)
        return SimulatedMower(
            mower_id=registration.mower_id,
            account_id=registration.account_id,
            mower_map=LAWN,
            config=SimulationConfig(time_scale=1.0, state_period_s=0.5),
        )

    return make_app(
        bus=bus,
        provider=TestProvider({reg.mower_id: factory}, tick_seconds=0.05),
        bootstrap_mowers=[reg.model_dump(mode="json")],
        telemetry_heartbeat_seconds=1.0,
    )


def _event(reg: MowerRegistration) -> EventEnvelope[MowerRegistration]:
    return EventEnvelope[MowerRegistration](
        type=MOWER_UPDATED, producer="fleet", account_id=reg.account_id, payload=reg
    )


async def test_dg_calibration_accepted_rule_13_and_stop() -> None:
    bus = InMemoryBus()
    reg = registration()
    async with lifespan_client(_app(bus, reg, [])):  # type: ignore[arg-type]
        await _leave_the_dock(bus, reg)
        calibrate = None
        for _ in range(100):  # the zero frame's drive session may still be open
            calibrate = envelope(reg, DriveCalibrate(step_s=1.0))
            await publish_command(bus, calibrate)
            command_id = calibrate.command_id
            await wait_until(functools.partial(_results_of, bus, reg, command_id))
            first = [r for r in results(bus, reg.mower_id) if r.command_id == calibrate.command_id]
            if first[0].status is CommandStatus.ACCEPTED:
                break
            assert "drive session is open" in (first[0].reason or ""), first[0].reason
        assert calibrate is not None
        await wait_until(
            lambda: any(
                s.drive_calibration is not None and s.drive_calibration.state == "measuring"
                for s in telemetry(bus, reg.mower_id)
            )
        )
        start = envelope(reg, StartMow(area_ids=["lawn"]))
        await publish_command(bus, start)
        await wait_until(lambda: _results_of(bus, reg, start.command_id))
        (refused,) = [r for r in results(bus, reg.mower_id) if r.command_id == start.command_id]
        assert (refused.status, refused.reason) == (CommandStatus.REJECTED, CALIBRATION_ACTIVE)
        stop = envelope(reg, SpotStop())
        await publish_command(bus, stop)
        await wait_until(lambda: len(_results_of(bus, reg, calibrate.command_id)) == 2)
        assert _results_of(bus, reg, stop.command_id) == ["executed"]
        final = [r for r in results(bus, reg.mower_id) if r.command_id == calibrate.command_id]
        assert final[-1].status is CommandStatus.FAILED
        assert "stopped_by_user" in (final[-1].reason or "")

        def aborted() -> bool:
            last = telemetry(bus, reg.mower_id)[-1].drive_calibration
            return last is not None and last.state == "aborted"

        await wait_until(aborted)
        assert telemetry(bus, reg.mower_id)[-1].activity is MowerActivity.IDLE


async def test_dg_calibration_is_taken_off_the_options_and_used_by_spot_runs() -> None:
    bus = InMemoryBus()
    reg = registration(options={"state_period_s": 0.5})
    seen: list[MowerRegistration] = []
    app = _app(bus, reg, seen)
    async with lifespan_client(app):  # type: ignore[arg-type]
        gateway = app.state.gateway  # type: ignore[attr-defined]
        await wait_until(lambda: seen and telemetry(bus, reg.mower_id))
        stored = reg.model_copy(
            update={"connection_options": {**reg.connection_options, **CALIBRATION.to_options()}}
        )
        await publish_event(bus, _event(stored))
        session = gateway.session(reg.mower_id)
        assert session is not None and session.registration == stored
        assert len(seen) == 1, "a calibration alone does not reopen the connection"
        controller = session._controller
        assert controller is not None
        config = controller.spot_config(controller.last_state)
        assert config.assumed_full_speed_mps == CALIBRATION.full_speed_mps
        assert config.dead_zone == CALIBRATION.dead_zone and config.max_linear == 1.0


def test_dg_calibration_rules() -> None:
    reg = registration(options={"battery_percent": 80})
    bridge = {"host": "bridge.local", "ble_address": "AA:BB:CC:DD:EE:FF"}
    stored = reg.model_copy(
        update={"links": {"ble_bridge": {**bridge, **CALIBRATION.to_options()}}}
    )
    assert drive_calibration_of(stored) == CALIBRATION
    assert drive_calibration_of(reg) is None
    stripped = without_calibration(stored)
    assert stripped.links == {"ble_bridge": bridge}
    assert stripped.connection_options == {"battery_percent": 80}
    assert same_link(reg.model_copy(update={"links": {"ble_bridge": bridge}}), stored)
    moved = stored.model_copy(update={"links": {"ble_bridge": {**bridge, "port": 6054}}})
    assert not same_link(stored, moved)
