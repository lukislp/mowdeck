"""The drive link of spot runs through the gateway, against a simulated mower with a Bluetooth
bridge: a signal below ``LINK_MIN_RSSI_DBM`` at the bridge refuses the start with the reason,
and a bridge that drops during a run ends it ``link_lost``. No manufacturer package is
imported."""

import functools
from collections.abc import Callable
from datetime import timedelta

from dg_support import TestProvider, envelope, make_app, registration, results, telemetry
from dg_support import wait_until as _wait_until
from mowdeck_contracts.v1 import (
    LINK_BLE_BRIDGE,
    CommandResult,
    CommandStatus,
    DockPose,
    GeoReference,
    LocalPoint,
    ManualDrive,
    MapArea,
    MowerActivity,
    MowerMap,
    MowerRegistration,
    SpotMow,
    SpotMowPlan,
    SpotSegment,
)
from mowdeck_control import MowerConnection, SimulatedMower, SimulationConfig
from mowdeck_service_kit.bus import InMemoryBus, publish_command
from mowdeck_service_kit.testing import lifespan_client

LAWN = MowerMap(
    reference=GeoReference(origin_lat=48.1, origin_lon=11.5),
    areas=[MapArea(id="lawn", boundary=[(0, 0), (12, 0), (12, 12), (0, 12)])],
    dock=DockPose(x=3.0, y=6.0, heading_deg=0.0),
)


async def wait_until(predicate: Callable[[], object], *, within: float = 20.0) -> None:
    await _wait_until(predicate, within=within)


def plan_of(*points: LocalPoint) -> SpotMowPlan:
    segment = SpotSegment.from_points(list(points), blades_on=False, speed_mps=0.3)
    return SpotMowPlan.build([segment], ["spiral"])


def _app(
    bus: InMemoryBus, reg: MowerRegistration, mowers: list[SimulatedMower], *, rssi: int
) -> object:
    def factory(registration: MowerRegistration) -> MowerConnection:
        mower = SimulatedMower(
            mower_id=registration.mower_id,
            account_id=registration.account_id,
            mower_map=LAWN,
            config=SimulationConfig(time_scale=1.0, bridge=True, bridge_rssi=rssi),
        )
        mowers.append(mower)
        return mower

    return make_app(
        bus=bus,
        provider=TestProvider({reg.mower_id: factory}, tick_seconds=0.05),
        bootstrap_mowers=[reg.model_dump(mode="json")],
        telemetry_heartbeat_seconds=1.0,
        path_progress_seconds=0.1,
    )


def _results_of(bus: InMemoryBus, reg: MowerRegistration, command_id: object) -> list[str]:
    return [r.status.value for r in results(bus, reg.mower_id) if r.command_id == command_id]


async def _leave_the_dock(bus: InMemoryBus, reg: MowerRegistration) -> None:
    await wait_until(lambda: telemetry(bus, reg.mower_id))
    frame = ManualDrive(linear=0.0, angular=0.0, ttl_ms=100)
    await publish_command(bus, envelope(reg, frame, ttl=timedelta(seconds=1)))
    await wait_until(
        lambda: any(s.activity is MowerActivity.IDLE for s in telemetry(bus, reg.mower_id))
    )


async def _send_spot(bus: InMemoryBus, reg: MowerRegistration, command: SpotMow) -> CommandResult:
    """Send ``command`` until the open drive session of leaving the dock is gone; the first
    result that is not about it."""
    for _ in range(100):
        spot = envelope(reg, command)
        await publish_command(bus, spot)
        command_id = spot.command_id
        await wait_until(functools.partial(_results_of, bus, reg, command_id))
        (first,) = [r for r in results(bus, reg.mower_id) if r.command_id == command_id]
        if "drive session is open" not in (first.reason or ""):
            return first
    raise AssertionError("the drive session never closed")


async def test_dg_spot_run_refused_on_a_weak_bridge_signal() -> None:
    bus = InMemoryBus()
    reg = registration()
    mowers: list[SimulatedMower] = []
    async with lifespan_client(_app(bus, reg, mowers, rssi=-97)):  # type: ignore[arg-type]
        await _leave_the_dock(bus, reg)
        state = telemetry(bus, reg.mower_id)[-1]
        assert state.links[LINK_BLE_BRIDGE].rssi == -97
        first = await _send_spot(bus, reg, SpotMow(plan=plan_of((3.0, 6.0), (4.0, 6.0))))
        assert first.status is CommandStatus.REJECTED
        assert "-97 dBm" in (first.reason or "")


async def test_dg_spot_run_ends_link_lost_when_the_bridge_drops() -> None:
    bus = InMemoryBus()
    reg = registration()
    mowers: list[SimulatedMower] = []
    async with lifespan_client(_app(bus, reg, mowers, rssi=-74)):  # type: ignore[arg-type]
        await _leave_the_dock(bus, reg)
        command = SpotMow(plan=plan_of((3.0, 6.0), (9.0, 6.0)), return_to_dock=False)
        first = await _send_spot(bus, reg, command)
        assert first.status is CommandStatus.ACCEPTED, first.reason
        command_id = first.command_id
        await wait_until(
            lambda: any(
                s.spot_run is not None and s.spot_run.state == "spot_mowing"
                for s in telemetry(bus, reg.mower_id)
            )
        )
        mowers[-1].set_bridge(False)
        await wait_until(lambda: len(_results_of(bus, reg, command_id)) == 2)
        final = [r for r in results(bus, reg.mower_id) if r.command_id == command_id][-1]
        assert final.status is CommandStatus.FAILED
        assert "link_lost" in (final.reason or ""), final.reason
        assert "frame_rejected" not in (final.reason or "")
