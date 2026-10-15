"""Spot runs through the gateway (ADR 0039) against the simulator only: the ``accepted`` result
at once and the final one at the run's end, ``MowerState.spot_run`` in the telemetry, the plan
as the path snapshot, rule 12 and ``spot_stop``. No manufacturer package is imported."""

import functools
from collections.abc import Callable
from datetime import timedelta

from dg_support import TestProvider, envelope, make_app, registration, results, telemetry
from dg_support import wait_until as _wait_until
from mowdeck_contracts.v1 import (
    CommandEnvelope,
    CommandStatus,
    DockPose,
    GeoReference,
    LocalPoint,
    ManualDrive,
    MapArea,
    MowerActivity,
    MowerMap,
    MowerPathSnapshot,
    MowerRegistration,
    SetLight,
    SpotMow,
    SpotMowPlan,
    SpotSegment,
    SpotStop,
    StartMow,
)
from mowdeck_contracts.v1.subjects import device_subject
from mowdeck_control import MowerConnection, SimulatedMower, SimulationConfig
from mowdeck_control.controller import SPOT_RUN_ACTIVE
from mowdeck_service_kit.bus import InMemoryBus, publish_command
from mowdeck_service_kit.testing import lifespan_client

LAWN = MowerMap(
    reference=GeoReference(origin_lat=48.1, origin_lon=11.5),
    areas=[MapArea(id="lawn", boundary=[(0, 0), (12, 0), (12, 12), (0, 12)])],
    dock=DockPose(x=3.0, y=6.0, heading_deg=0.0),
)
"""A lawn with the dock inside it, so the simulated mower starts within the geofence."""


async def wait_until(predicate: Callable[[], object], *, within: float = 20.0) -> None:
    """Generous: the run drives in real time and CI runners are slow."""
    await _wait_until(predicate, within=within)


def plan_of(*points: LocalPoint) -> SpotMowPlan:
    segment = SpotSegment.from_points(list(points), blades_on=False, speed_mps=0.3)
    return SpotMowPlan.build([segment], ["spiral"])


def _provider(reg: MowerRegistration) -> TestProvider:
    def factory(registration: MowerRegistration) -> MowerConnection:
        return SimulatedMower(
            mower_id=registration.mower_id,
            account_id=registration.account_id,
            mower_map=LAWN,
            config=SimulationConfig(time_scale=1.0),
        )

    return TestProvider({reg.mower_id: factory}, tick_seconds=0.05)


def _paths(bus: InMemoryBus, reg: MowerRegistration) -> list[MowerPathSnapshot]:
    subject = device_subject(reg.mower_id, "path")
    return [m.model(MowerPathSnapshot) for m in bus.messages(subject)]


def _statuses(bus: InMemoryBus, reg: MowerRegistration, command_id: object) -> list[str]:
    return [
        s.spot_run.state
        for s in telemetry(bus, reg.mower_id)
        if s.spot_run is not None and s.spot_run.run_id == command_id
    ]


def _app(bus: InMemoryBus, reg: MowerRegistration) -> object:
    return make_app(
        bus=bus,
        provider=_provider(reg),
        bootstrap_mowers=[reg.model_dump(mode="json")],
        telemetry_heartbeat_seconds=1.0,
        path_progress_seconds=0.1,
    )


async def _leave_the_dock(bus: InMemoryBus, reg: MowerRegistration) -> None:
    """A short zero frame opens a drive session (the mower stands idle on the lawn); its
    dead-man closes it again."""
    await wait_until(lambda: telemetry(bus, reg.mower_id))
    frame = ManualDrive(linear=0.0, angular=0.0, ttl_ms=100)
    await publish_command(bus, envelope(reg, frame, ttl=timedelta(seconds=1)))
    await wait_until(
        lambda: any(s.activity is MowerActivity.IDLE for s in telemetry(bus, reg.mower_id))
    )


def _results_of(bus: InMemoryBus, reg: MowerRegistration, command_id: object) -> list[str]:
    return [r.status.value for r in results(bus, reg.mower_id) if r.command_id == command_id]


async def _start_spot(
    bus: InMemoryBus, reg: MowerRegistration, command: SpotMow
) -> CommandEnvelope:
    """Send ``command`` until it is accepted: right after leaving the dock the frame's drive
    session may still be open (the run refuses to start then); no wall-clock sleep."""
    for _ in range(100):
        spot = envelope(reg, command)
        await publish_command(bus, spot)
        command_id = spot.command_id
        await wait_until(functools.partial(_results_of, bus, reg, command_id))
        (first,) = [r for r in results(bus, reg.mower_id) if r.command_id == command_id]
        if first.status is CommandStatus.ACCEPTED:
            return spot
        assert "drive session is open" in (first.reason or ""), first.reason
    raise AssertionError("the spot run never started")


async def test_dg_spot_run_publishes_accepted_then_executed() -> None:
    bus = InMemoryBus()
    reg = registration()
    async with lifespan_client(_app(bus, reg)):  # type: ignore[arg-type]
        await _leave_the_dock(bus, reg)
        command = SpotMow(plan=plan_of((3.0, 6.0), (3.8, 6.0)), speed_mps=0.3)
        spot = await _start_spot(bus, reg, command)
        await wait_until(lambda: len(_results_of(bus, reg, spot.command_id)) == 2)
        accepted, final = [r for r in results(bus, reg.mower_id) if r.command_id == spot.command_id]
        assert accepted.status is CommandStatus.ACCEPTED and "dry run" in (accepted.reason or "")
        assert final.status is CommandStatus.EXECUTED, final.reason
        assert final.reason is not None and final.reason.startswith("spot run done")
        await wait_until(lambda: "done" in _statuses(bus, reg, spot.command_id))
        states = _statuses(bus, reg, spot.command_id)
        assert "spot_mowing" in states and states[-1] == "done"
        await wait_until(lambda: _paths(bus, reg) and _paths(bus, reg)[-1].path is None)
        planned = [s.path for s in _paths(bus, reg) if s.path is not None]
        assert planned and planned[0].kinds == ["mow"] and planned[0].points_total == 2


async def test_dg_spot_run_rule_12_and_spot_stop() -> None:
    bus = InMemoryBus()
    reg = registration()
    async with lifespan_client(_app(bus, reg)):  # type: ignore[arg-type]
        await _leave_the_dock(bus, reg)
        lonely = envelope(reg, SpotStop())
        await publish_command(bus, lonely)
        await wait_until(lambda: _results_of(bus, reg, lonely.command_id))
        assert _results_of(bus, reg, lonely.command_id) == ["rejected"]
        spot = await _start_spot(bus, reg, SpotMow(plan=plan_of((3.0, 6.0), (9.0, 6.0))))
        start = envelope(reg, StartMow(area_ids=["lawn"]))
        await publish_command(bus, start)
        await wait_until(lambda: _results_of(bus, reg, start.command_id))
        (refused,) = [r for r in results(bus, reg.mower_id) if r.command_id == start.command_id]
        assert (refused.status, refused.reason) == (CommandStatus.REJECTED, SPOT_RUN_ACTIVE)
        light = envelope(reg, SetLight(on=True))
        await publish_command(bus, light)
        await wait_until(lambda: _results_of(bus, reg, light.command_id))
        assert _results_of(bus, reg, light.command_id) == ["executed"]
        stop = envelope(reg, SpotStop())
        await publish_command(bus, stop)
        await wait_until(lambda: len(_results_of(bus, reg, spot.command_id)) == 2)
        assert _results_of(bus, reg, stop.command_id) == ["executed"]
        final = [r for r in results(bus, reg.mower_id) if r.command_id == spot.command_id][-1]
        assert final.status is CommandStatus.FAILED
        assert "stopped_by_user" in (final.reason or "")


GARDENS = MowerMap(
    reference=GeoReference(origin_lat=48.1, origin_lon=11.5),
    areas=[
        MapArea(id="lawn", boundary=[(0, 0), (12, 0), (12, 12), (0, 12)]),
        MapArea(id="far", boundary=[(200, 0), (212, 0), (212, 12), (200, 12)]),
    ],
    dock=DockPose(x=3.0, y=6.0, heading_deg=0.0),
)
"""Two gardens without a transfer path between them."""


def _gardens_app(bus: InMemoryBus, reg: MowerRegistration) -> object:
    def factory(registration: MowerRegistration) -> MowerConnection:
        return SimulatedMower(
            mower_id=registration.mower_id,
            account_id=registration.account_id,
            mower_map=GARDENS,
            config=SimulationConfig(time_scale=1.0),
        )

    return make_app(
        bus=bus,
        provider=TestProvider({reg.mower_id: factory}, tick_seconds=0.05),
        bootstrap_mowers=[reg.model_dump(mode="json")],
        telemetry_heartbeat_seconds=1.0,
        path_progress_seconds=0.1,
    )


async def test_dg_spot_run_without_a_route_is_rejected() -> None:
    bus = InMemoryBus()
    reg = registration()
    async with lifespan_client(_gardens_app(bus, reg)):  # type: ignore[arg-type]
        await wait_until(lambda: telemetry(bus, reg.mower_id))
        far = envelope(reg, SpotMow(plan=plan_of((205.0, 6.0), (206.0, 6.0))))
        await publish_command(bus, far)
        await wait_until(lambda: _results_of(bus, reg, far.command_id))
        (refused,) = [r for r in results(bus, reg.mower_id) if r.command_id == far.command_id]
        assert refused.status is CommandStatus.REJECTED
        assert (refused.reason or "").startswith("no_route:"), refused.reason
        assert all(s.activity is not MowerActivity.IDLE for s in telemetry(bus, reg.mower_id))


async def test_dg_spot_run_starts_at_the_dock() -> None:
    bus = InMemoryBus()
    reg = registration()
    async with lifespan_client(_gardens_app(bus, reg)):  # type: ignore[arg-type]
        await wait_until(lambda: telemetry(bus, reg.mower_id))
        spot = envelope(reg, SpotMow(plan=plan_of((6.0, 6.0), (7.0, 6.0))))
        await publish_command(bus, spot)
        await wait_until(lambda: _results_of(bus, reg, spot.command_id))
        (accepted,) = [r for r in results(bus, reg.mower_id) if r.command_id == spot.command_id]
        assert accepted.status is CommandStatus.ACCEPTED, accepted.reason
        assert "leaving the dock" in (accepted.reason or "")
        await wait_until(lambda: "undocking" in _statuses(bus, reg, spot.command_id))
        stop = envelope(reg, SpotStop())
        await publish_command(bus, stop)
        await wait_until(lambda: len(_results_of(bus, reg, spot.command_id)) == 2)
        final = [r for r in results(bus, reg.mower_id) if r.command_id == spot.command_id][-1]
        assert final.status is CommandStatus.FAILED and "stopped_by_user" in (final.reason or "")
