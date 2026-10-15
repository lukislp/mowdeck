"""Spot runs per turn style (ADR 0043): the Planner's stripes with shaped lane ends, driven by
the generic spot run on the simulator at 1 Hz reports with 2 cm noise. Arc and multi-point
stripes need no rotation on the spot; the three-point turns reverse with the blades off and
stay inside the geofence. No manufacturer package is imported."""

import asyncio
import math
from datetime import timedelta
from uuid import uuid4

import pytest
from mowdeck_contracts.v1 import (
    CommandEnvelope,
    CommandStatus,
    DockPose,
    GeoReference,
    ManualDrive,
    MapArea,
    MapZone,
    MowerCommand,
    MowerMap,
    PrincipalKind,
    PrincipalRef,
    SpotMow,
    SpotMowPlan,
    TurnStyle,
)
from mowdeck_control import (
    Geofence,
    MowerController,
    SimulatedMower,
    SimulationConfig,
    SpotRunReport,
)
from mowdeck_control.testing import ManualClock, ManualTimers
from mowdeck_patterns import Area, MowerSpec, TurnOptions, plan

from planner.application.planning import spot_geofence

ISSUER = PrincipalRef(subject=uuid4(), kind=PrincipalKind.USER)
MAP = MowerMap(
    reference=GeoReference(origin_lat=48.1, origin_lon=11.5, rotation_deg=20.0),
    areas=[MapArea(id="lawn", boundary=[(0, 0), (12, 0), (12, 12), (0, 12)])],
    no_go_zones=[MapZone(id="pond", boundary=[(9, 9), (11, 9), (11, 11), (9, 11)])],
    dock=DockPose(x=3.0, y=6.0, heading_deg=0.0),
)
"""The lawn of the control tests; the simulated mower stands at (3, 6) heading +x."""
SPOT = [(2.79, 5.79), (5.79, 5.79), (5.79, 7.59), (2.79, 7.59)]
"""Its first stripe starts where the mower stands (3.0, 6.0) and runs in +x."""
MOWER = MowerSpec(cutting_width_m=0.4, overlap_m=0.05, max_speed_mps=0.3)


def stripes(style: TurnStyle | None, *, reverse: bool = True) -> SpotMowPlan:
    area = Area.from_map(MAP, "lawn", boundary=SPOT)
    turns = (
        TurnOptions(style=style.value, reverse=reverse, fence=spot_geofence(MAP))
        if style is not None
        else None
    )
    result = plan(area, MOWER, [("stripes", {"angle_deg": 0})], turns=turns)
    return result.to_spot_plan()


class Harness:
    """A controller on a simulated mower in virtual time (the run's sleeps tick it)."""

    def __init__(self, config: SimulationConfig) -> None:
        self.clock = ManualClock()
        self.timers = ManualTimers(self.clock)
        self.sim = SimulatedMower(mower_map=MAP, config=config, clock=self.clock)
        self.controller = MowerController(
            self.sim,
            mower_id=self.sim.mower_id,
            clock=self.clock,
            timer_factory=self.timers,
            spot_sleep=self.sleep,
        )
        self.fence = Geofence.of(MAP)
        self.reversed_s = 0.0
        self.blades_while_reversing = False
        self.outside: list[tuple[float, float]] = []

    async def sleep(self, seconds: float) -> None:
        await self.timers.advance(seconds)
        self.sim.tick(seconds)
        command = self.sim._drive_cmd
        if command.linear < 0:
            self.reversed_s += seconds
            self.blades_while_reversing |= self.sim.blade_on
            if not self.fence.contains(self.sim.local_position):
                self.outside.append(self.sim.local_position)
        for _ in range(4):
            await asyncio.sleep(0)

    async def send(self, command: MowerCommand) -> CommandEnvelope:
        envelope = CommandEnvelope.create(
            mower_id=self.sim.mower_id,
            account_id=self.sim.account_id,
            issued_by=ISSUER,
            command=command,
            ttl=timedelta(seconds=30),
            now=self.clock(),
        )
        self.last = await self.controller.handle(envelope)
        return envelope

    async def run(self, command: SpotMow) -> SpotRunReport:
        await self.controller.start()
        await self.send(ManualDrive(linear=0, angular=0))  # out of the dock, idle
        await self.controller.end_drive_session()
        envelope = await self.send(command)
        assert self.last.status is CommandStatus.ACCEPTED, self.last.reason
        report = await self.controller.spot_run_result(envelope.command_id)
        assert report is not None
        await self.controller.close()
        return report


def sim_config(**changes: object) -> SimulationConfig:
    return SimulationConfig(
        state_period_s=1.0,
        position_noise_m=0.02,
        **changes,  # type: ignore[arg-type]
    )


@pytest.mark.parametrize("style", [TurnStyle.ARC, TurnStyle.MULTI_POINT])
async def test_stripes_with_shaped_turns_never_rotate_on_the_spot(style: TurnStyle) -> None:
    h = Harness(sim_config())
    report = await h.run(
        SpotMow(
            plan=stripes(style),
            speed_mps=0.2,
            turn_style=style,
            blades=True,
            confirm_blades=True,
            return_to_dock=False,
        )
    )
    assert report.state == "done", report.summary()
    assert report.turn_style is style
    assert report.in_place_turns == 0, report.summary()
    assert report.coverage_percent >= 90
    assert "turns " + style.value in (h.last.reason or "")
    if style is TurnStyle.MULTI_POINT:
        assert h.reversed_s > 0, "the three-point turns never reversed"
        assert not h.blades_while_reversing
        assert h.outside == []
    else:
        assert h.reversed_s == 0


async def test_in_place_style_rotates_at_every_lane_end() -> None:
    h = Harness(sim_config())
    report = await h.run(
        SpotMow(
            plan=stripes(TurnStyle.IN_PLACE),
            speed_mps=0.2,
            turn_style=TurnStyle.IN_PLACE,
            return_to_dock=False,
        )
    )
    assert report.state == "done", report.summary()
    assert report.turn_style is TurnStyle.IN_PLACE
    assert report.in_place_turns >= 4  # two rotations per lane end, some merge at 1 Hz


async def test_the_mowers_own_setting_is_the_default_and_multi_point_needs_reverse() -> None:
    # the mower reports multi-point as its own setting, but this drive link cannot reverse
    h = Harness(sim_config(turn_style=TurnStyle.MULTI_POINT, drive_reverse=False))
    report = await h.run(
        SpotMow(plan=stripes(TurnStyle.MULTI_POINT, reverse=False), return_to_dock=False)
    )
    assert report.state == "done", report.summary()
    assert report.turn_style is TurnStyle.ARC
    assert report.in_place_turns == 0
    assert h.reversed_s == 0


async def test_a_plan_that_reverses_is_refused_on_a_link_without_reverse() -> None:
    h = Harness(sim_config(drive_reverse=False))
    await h.controller.start()
    await h.send(ManualDrive(linear=0, angular=0))
    await h.controller.end_drive_session()
    await h.send(SpotMow(plan=stripes(TurnStyle.MULTI_POINT), turn_style=TurnStyle.MULTI_POINT))
    assert h.last.status is CommandStatus.REJECTED
    assert "cannot reverse" in (h.last.reason or "")
    await h.controller.close()


def test_three_point_turns_stay_inside_the_geofence_on_paper() -> None:
    spot = stripes(TurnStyle.MULTI_POINT)
    fence = Geofence.of(MAP)
    reverse = [s for s in spot.segments if s.reverse]
    assert reverse and all(not s.blades_on for s in reverse)
    for segment in spot.segments:
        for a, b in zip(segment.points(), segment.points()[1:], strict=False):
            assert fence.segment_clear(a, b, step_m=0.02)
    assert math.isclose(len(reverse), sum(1 for s in spot.segments if s.kind == "mow") - 1)
