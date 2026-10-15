"""Spot runs (ADR 0039) against the simulator only: convergence on a spiral at 1 Hz reports with
2 cm noise, the RTK pause, aborts (geofence, sign error, timeout, link), STOP with blades off
and the controller's rule 12. No manufacturer package is imported."""

import asyncio
import itertools
import math
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
from mowdeck_contracts.v1 import (
    CommandEnvelope,
    CommandResult,
    CommandStatus,
    ConnectionKind,
    DockPose,
    EmergencyStop,
    GeoReference,
    LocalPoint,
    ManualDrive,
    MapArea,
    MapZone,
    MowerActivity,
    MowerCommand,
    MowerMap,
    MowerState,
    PrincipalKind,
    PrincipalRef,
    RtkFixStatus,
    SetLight,
    SpotMow,
    SpotMowPlan,
    SpotRunStatus,
    SpotSegment,
    SpotStop,
    StartMow,
)
from mowdeck_control import (
    DrivableSpace,
    Geofence,
    MowerController,
    SimulatedMower,
    SimulationConfig,
    SpotMowRun,
    SpotRunConfig,
    SpotRunReport,
    default_map,
    to_geo_position,
)
from mowdeck_control.controller import SPOT_RUN_ACTIVE
from mowdeck_control.geo import segment_distance
from mowdeck_control.spot_run import (
    REASON_EMERGENCY_STOP,
    REASON_GEOFENCE,
    REASON_HEADING_MISMATCH,
    REASON_LINK_LOST,
    REASON_RTK_LOST,
    REASON_STOPPED_BY_USER,
    REASON_TIMEOUT,
    SPOT_START_MAX_STATE_AGE_S,
    spot_start_problem,
)
from mowdeck_control.testing import ManualClock, ManualTimers
from mowdeck_control.transit import REASON_NO_ROUTE

ISSUER = PrincipalRef(subject=uuid4(), kind=PrincipalKind.USER)
MAP = MowerMap(
    reference=GeoReference(origin_lat=48.1, origin_lon=11.5, rotation_deg=20.0),
    areas=[MapArea(id="lawn", boundary=[(0, 0), (12, 0), (12, 12), (0, 12)])],
    no_go_zones=[MapZone(id="pond", boundary=[(9, 9), (11, 9), (11, 11), (9, 11)])],
    dock=DockPose(x=3.0, y=6.0, heading_deg=0.0),
)
"""A 12 x 12 m lawn; the dock (where the simulated mower starts) lies inside it."""


def plan_of(*lines: list[LocalPoint], blades: bool = False, speed: float = 0.2) -> SpotMowPlan:
    segments = [
        SpotSegment.from_points(points, blades_on=blades, speed_mps=speed) for points in lines
    ]
    return SpotMowPlan.build(segments, ["spiral"])


def spiral(
    center: LocalPoint, r0: float = 0.3, r1: float = 1.5, pitch: float = 0.3
) -> list[LocalPoint]:
    """An Archimedean spiral from ``r0`` to ``r1`` with ``pitch`` metres between turns and
    points about 0.3 m apart."""
    points: list[LocalPoint] = []
    phi = 0.0
    while True:
        r = r0 + pitch * phi / (2 * math.pi)
        if r > r1:
            break
        points.append((center[0] + r * math.cos(phi), center[1] + r * math.sin(phi)))
        phi += 0.3 / r
    return points


class Harness:
    """A controller on a simulated mower in virtual time: the run's sleeps advance the manual
    clock and tick the simulator, so a 1 Hz report rate and noise are exact and no test sleeps.
    """

    def __init__(
        self,
        *,
        config: SimulationConfig | None = None,
        spot_config: SpotRunConfig | None = None,
        mower_map: MowerMap = MAP,
    ) -> None:
        self.clock = ManualClock()
        self.timers = ManualTimers(self.clock)
        self.sim = SimulatedMower(
            mower_map=mower_map,
            config=config or SimulationConfig(state_period_s=1.0, position_noise_m=0.02),
            clock=self.clock,
        )
        self.statuses: list[SpotRunStatus] = []
        self.controller = MowerController(
            self.sim,
            mower_id=self.sim.mower_id,
            clock=self.clock,
            timer_factory=self.timers,
            spot_sleep=self.sleep,
            spot_config=spot_config,
            on_state=self._on_state,
        )
        self.track: list[LocalPoint] = []
        self.blade_seen = False
        # every status the run reports, also those a quicker one replaced before it was emitted
        self.run_states: list[str] = []
        publish = self.controller._on_spot_status

        def record(status: SpotRunStatus) -> None:
            if not self.run_states or self.run_states[-1] != status.state:
                self.run_states.append(status.state)
            publish(status)

        self.controller._on_spot_status = record  # type: ignore[method-assign]

    def _on_state(self, state: MowerState) -> None:
        if state.spot_run is not None and (
            not self.statuses or self.statuses[-1] != state.spot_run
        ):
            self.statuses.append(state.spot_run)

    async def sleep(self, seconds: float) -> None:
        await self.timers.advance(seconds)
        self.sim.tick(seconds)
        self.track.append(self.sim.local_position)
        self.blade_seen = self.blade_seen or self.sim.blade_on
        await self.settle()

    async def until(self, condition: Callable[[], bool], within_s: float, what: str) -> None:
        """Yield to the run until ``condition`` holds; fails after ``within_s`` virtual seconds
        (the run's own sleeps advance the clock, so this never waits on the wall clock)."""
        start = self.clock()
        for _ in range(1_000_000):
            if condition():
                return
            assert (self.clock() - start).total_seconds() < within_s, what
            await asyncio.sleep(0)
        raise AssertionError(what)

    @staticmethod
    async def settle() -> None:
        for _ in range(4):
            await asyncio.sleep(0)

    async def start(self, *, idle: bool = True) -> None:
        await self.controller.start()
        await self.settle()
        if idle:
            # A zero frame opens a drive session (the mower leaves the dock state and stands
            # idle on the lawn); ending it leaves the mower idle where it is.
            assert (await self.send(ManualDrive(linear=0, angular=0))).status == "accepted"
            await self.controller.end_drive_session()
            await self.settle()

    async def send(self, command: MowerCommand, **changes: Any) -> CommandResult:
        envelope = CommandEnvelope.create(
            mower_id=self.sim.mower_id,
            account_id=self.sim.account_id,
            issued_by=ISSUER,
            command=command,
            ttl=timedelta(seconds=30),
            now=self.clock(),
            **changes,
        )
        return await self.controller.handle(envelope)

    async def run(self, command: SpotMow) -> tuple[CommandResult, SpotRunReport | None]:
        result = await self.send(command)
        if result.status is not CommandStatus.ACCEPTED:
            return result, None
        report = await self.controller.spot_run_result(result.command_id)
        await self.settle()  # the final status is published by a task
        return result, report

    def max_deviation(self, lines: list[list[LocalPoint]]) -> float:
        legs = [(a, b) for line in lines for a, b in itertools.pairwise(line)]
        return max(min(segment_distance(p, a, b) for a, b in legs) for p in self.track)

    async def close(self) -> None:
        await self.controller.close()


# --- convergence and pauses on the simulator --------------------------------------------------


async def test_control_spot_run_converges_on_a_spiral_at_1hz_with_noise() -> None:
    h = Harness()
    await h.start()
    line = spiral((7.0, 6.0))
    plan = plan_of(line)
    approach = [h.sim.local_position, line[0]]
    result, report = await h.run(SpotMow(plan=plan, speed_mps=0.2))
    assert result.status is CommandStatus.ACCEPTED and "dry run" in (result.reason or "")
    assert report is not None, result
    assert report.state == "done", report.summary()
    assert report.driven_points == report.planned_points == len(line)
    assert report.coverage_percent >= 90
    assert report.dry_run and not h.blade_seen
    assert h.max_deviation([approach, line]) < 0.5, "the true track left the planned path"
    assert 60 < report.duration_s < 20 * 60
    states = h.run_states
    assert states[0] == "transit" and "spot_mowing" in states and states[-1] == "done"
    assert states[-2] == "returning" and report.sent_home, "the mower was not sent home"
    assert h.statuses[-1].state == "done"
    assert h.statuses[-1].progress_percent == 100
    assert not h.controller.drive_session_active and not h.controller.spot_run_active
    final = h.controller.last_state
    assert final is not None and final.spot_run is not None and final.spot_run.state == "done"
    await h.close()


async def test_control_spot_run_pauses_on_rtk_float_and_resumes() -> None:
    h = Harness()
    await h.start()
    plan = plan_of([(3.5, 6.0), (6.5, 6.0)])
    started = await h.send(SpotMow(plan=plan))
    assert started.status is CommandStatus.ACCEPTED
    await h.until(lambda: h.sim.local_position[0] >= 4.0, 30, "the mower did not move")
    h.sim.set_rtk(RtkFixStatus.FLOAT)
    await h.settle()
    position = h.sim.local_position
    paused_at = h.clock()

    def paused() -> bool:
        status = h.controller.spot_status
        return status is not None and status.state == "paused_rtk"

    await h.until(paused, 3, "the run did not pause")
    status = h.controller.spot_status
    assert status is not None and status.reason == "rtk_float"
    await h.until(lambda: (h.clock() - paused_at).total_seconds() >= 5, 10, "no time passed")
    assert math.dist(position, h.sim.local_position) < 0.05, "the mower moved while paused"
    assert h.controller.drive_session_active, "zero frames keep the session open"
    h.sim.set_rtk(RtkFixStatus.FIXED)
    report = await h.controller.spot_run_result(started.command_id)
    assert report is not None and report.state == "done", report and report.summary()
    assert report.paused_s >= 5
    assert "paused_rtk" in [s.state for s in h.statuses]
    await h.close()


async def test_control_spot_run_aborts_after_the_rtk_wait() -> None:
    h = Harness(spot_config=SpotRunConfig(rtk_wait_s=6.0))
    await h.start()
    started = await h.send(SpotMow(plan=plan_of([(3.5, 6.0), (6.5, 6.0)])))
    assert started.status is CommandStatus.ACCEPTED
    await h.settle()
    h.sim.set_rtk(RtkFixStatus.NONE)
    report = await h.controller.spot_run_result(started.command_id)
    assert report is not None and (report.state, report.reason) == ("aborted", REASON_RTK_LOST)
    assert not h.controller.drive_session_active
    await h.close()


# --- aborts ---------------------------------------------------------------------------------


async def test_control_spot_run_aborts_on_a_turn_direction_sign_error() -> None:
    inverted = SimulationConfig(state_period_s=1.0, drive_max_turn_rad_s=-1.0)
    h = Harness(config=inverted)
    await h.start()
    _, report = await h.run(SpotMow(plan=plan_of([(3.0, 6.5), (3.0, 8.0)])))
    assert report is not None
    assert (report.state, report.reason) == ("aborted", REASON_HEADING_MISMATCH)
    assert report.detail and "turning" in report.detail
    assert report.duration_s < 10
    await h.close()


async def test_control_spot_run_aborts_on_the_time_limit() -> None:
    stuck_turning = SimulationConfig(state_period_s=1.0, drive_max_turn_rad_s=0.0)
    h = Harness(config=stuck_turning)
    await h.start()
    plan = plan_of([(3.0, 6.5), (3.0, 8.0)])
    _, report = await h.run(SpotMow(plan=plan))
    assert report is not None
    assert (report.state, report.reason) == ("aborted", REASON_TIMEOUT)
    # the plan's share (2 x 1.5 m / 0.2 m/s + 60 s) plus twice the transit's 0.5 m at 0.3 m/s
    assert report.duration_s == pytest.approx(2 * 1.5 / 0.2 + 60 + 2 * 0.5 / 0.3, abs=1.0)
    await h.close()


async def test_control_spot_run_stop_ends_with_blades_off() -> None:
    h = Harness(config=SimulationConfig(state_period_s=1.0, manual_drive_blades=True))
    await h.start()
    plan = plan_of([(3.0, 6.0), (9.0, 6.0)], blades=True)
    started = await h.send(SpotMow(plan=plan, blades=True, confirm_blades=True))
    assert started.status is CommandStatus.ACCEPTED and "blades" in (started.reason or "")
    await h.until(lambda: h.sim.blade_on, 10, "the blades never started")
    assert h.controller.spot_run_active
    path = h.controller.current_path()
    assert path is not None and path.kinds == ["mow"] and path.points_total == 2
    stopped = await h.send(SpotStop())
    assert stopped.status is CommandStatus.EXECUTED, stopped.reason
    assert not h.sim.blade_on and not h.controller.drive_session_active
    report = await h.controller.spot_run_result(started.command_id)
    assert report is not None
    assert (report.state, report.reason) == ("aborted", REASON_STOPPED_BY_USER)
    assert not report.dry_run
    assert h.controller.current_path() is None
    await h.close()


async def test_control_spot_run_emergency_stop_ends_the_run() -> None:
    h = Harness()
    await h.start()
    started = await h.send(SpotMow(plan=plan_of([(3.0, 6.0), (9.0, 6.0)])))
    assert started.status is CommandStatus.ACCEPTED
    await h.settle()
    estop = await h.send(EmergencyStop())
    assert estop.status in (CommandStatus.EXECUTED, CommandStatus.ACCEPTED)
    report = await h.controller.spot_run_result(started.command_id)
    assert report is not None and report.reason == REASON_EMERGENCY_STOP
    assert not h.controller.spot_run_active
    await h.close()


# --- rule 12 and the start checks ---------------------------------------------------------------


async def test_control_spot_run_rule_12_rejects_other_commands_while_a_run_is_active() -> None:
    h = Harness()
    await h.start()
    assert (await h.send(SpotStop())).reason == "no spot run is active"
    started = await h.send(SpotMow(plan=plan_of([(3.0, 6.0), (9.0, 6.0)])))
    assert started.status is CommandStatus.ACCEPTED
    for command in (
        StartMow(area_ids=["lawn"]),
        ManualDrive(linear=0.5, angular=0.0),
        SpotMow(plan=plan_of([(3.0, 6.0), (4.0, 6.0)])),
    ):
        result = await h.send(command)
        assert (result.status, result.reason) == (CommandStatus.REJECTED, SPOT_RUN_ACTIVE)
    light = await h.send(SetLight(on=True))
    assert light.status is CommandStatus.EXECUTED
    assert h.controller.spot_run_active, "the light did not end the run"
    assert (await h.send(SpotStop())).status is CommandStatus.EXECUTED
    await h.close()


async def test_control_spot_run_start_checks() -> None:
    docked = Harness(config=SimulationConfig(state_period_s=1.0, undock=False))
    await docked.start(idle=False)
    refused = await docked.send(SpotMow(plan=plan_of([(3.0, 6.0), (4.0, 6.0)])))
    assert refused.status is CommandStatus.REJECTED and "docked" in (refused.reason or "")
    assert "cannot undock" in (refused.reason or "")
    await docked.close()

    h = Harness()
    await h.start()
    outside = await h.send(SpotMow(plan=plan_of([(3.0, 6.0), (13.0, 6.0)])))
    assert outside.status is CommandStatus.REJECTED
    assert "leaves the geofence" in (outside.reason or "")
    near_pond = await h.send(SpotMow(plan=plan_of([(3.0, 6.0), (8.9, 8.9)])))
    assert "no-go zone" in (near_pond.reason or "")
    h.sim.set_rtk(RtkFixStatus.FLOAT)
    await h.settle()
    unfixed = await h.send(SpotMow(plan=plan_of([(3.0, 6.0), (4.0, 6.0)])))
    assert "no fixed RTK" in (unfixed.reason or "")
    await h.close()

    bladeless = Harness(config=SimulationConfig(state_period_s=1.0, manual_drive_blades=False))
    await bladeless.start()
    blades = SpotMow(
        plan=plan_of([(3.0, 6.0), (4.0, 6.0)], blades=True), blades=True, confirm_blades=True
    )
    forced = await bladeless.send(blades)
    assert forced.status is CommandStatus.ACCEPTED
    assert "blades forced off" in (forced.reason or "")
    report = await bladeless.controller.spot_run_result(forced.command_id)
    assert report is not None and report.dry_run and not bladeless.blade_seen
    await bladeless.close()


async def test_control_spot_run_without_manual_drive_is_rejected() -> None:
    h = Harness(config=SimulationConfig(bridge=True, bridge_connected=False))
    await h.controller.start()
    refused = await h.send(SpotMow(plan=plan_of([(3.0, 6.0), (4.0, 6.0)])))
    assert refused.status is CommandStatus.REJECTED
    assert "manual driving" in (refused.reason or "")
    await h.close()


# --- the run alone: a scripted pose source and a fake frame channel --------------------------


class FakeFrames:
    def __init__(self, refuse: str | None = None) -> None:
        self.frames: list[tuple[float, float, bool]] = []
        self.closed = 0
        self.refuse = refuse

    async def send(
        self, *, linear: float, angular: float, blade_on: bool, ttl_ms: int
    ) -> str | None:
        assert ttl_ms == 1000
        self.frames.append((linear, angular, blade_on))
        return self.refuse

    async def close(self) -> None:
        self.closed += 1


def state_at(
    x: float, y: float, theta: float, at: datetime, fix: RtkFixStatus = RtkFixStatus.FIXED
) -> MowerState:
    return MowerState(
        mower_id=UUID(int=1),
        account_id=UUID(int=2),
        observed_at=at,
        activity=MowerActivity.IDLE,
        battery_percent=80,
        position=to_geo_position(MAP.reference, x, y, theta),
        rtk_fix=fix,
        connection=ConnectionKind.SIMULATED,
    )


class Scripted:
    """A pose source and a sleep: every sleep advances the clock and moves the pose along a
    script of local poses (one per second)."""

    def __init__(self, clock: ManualClock, poses: list[tuple[float, float, float]]) -> None:
        self.clock = clock
        self.poses = poses
        self.index = 0
        self.elapsed = 0.0
        self.state = state_at(*poses[0], clock())

    def __call__(self) -> MowerState:
        return self.state

    async def sleep(self, seconds: float) -> None:
        self.clock.advance(seconds)
        self.elapsed += seconds
        due = min(int(self.elapsed), len(self.poses) - 1)
        if due != self.index:
            self.index = due
            self.state = state_at(*self.poses[due], self.clock())
        await asyncio.sleep(0)


def make_run(
    script: Scripted, frames: FakeFrames, plan: SpotMowPlan, clock: ManualClock
) -> SpotMowRun:
    run = SpotMowRun(
        run_id=uuid4(),
        plan=plan,
        mower_map=MAP,
        pose_source=script,
        frames=frames,
        speed_mps=0.2,
        clock=clock,
        sleep=script.sleep,
    )
    assert run.prepare(script()) is None
    return run


async def test_control_spot_run_aborts_when_the_pose_leaves_the_geofence() -> None:
    clock = ManualClock()
    frames = FakeFrames()
    script = Scripted(clock, [(3.0, 6.0, 0.0), (3.4, 6.0, 0.0), (3.8, 6.0, 0.0), (0.1, 6.0, 0.0)])
    report = await make_run(script, frames, plan_of([(3.0, 6.0), (6.0, 6.0)]), clock).run()
    assert (report.state, report.reason) == ("aborted", REASON_GEOFENCE)
    assert frames.closed == 1 and report.frames_sent >= 10
    assert all(not blade for _, _, blade in frames.frames)


async def test_control_spot_run_aborts_on_a_large_cross_track_error() -> None:
    clock = ManualClock()
    frames = FakeFrames()
    script = Scripted(clock, [(3.0, 6.0, 0.0), (3.4, 6.0, 0.0), (3.8, 7.5, 0.0)])
    report = await make_run(script, frames, plan_of([(3.0, 6.0), (6.0, 6.0)]), clock).run()
    assert (report.state, report.reason) == ("aborted", REASON_HEADING_MISMATCH)
    assert report.detail and "cross-track" in report.detail


async def test_control_spot_run_refused_frames_end_the_run() -> None:
    clock = ManualClock()
    frames = FakeFrames(refuse="failed: mower is not connected")
    script = Scripted(clock, [(3.0, 6.0, 0.0)])
    report = await make_run(script, frames, plan_of([(3.0, 6.0), (6.0, 6.0)]), clock).run()
    assert (report.state, report.reason) == ("aborted", REASON_LINK_LOST)
    assert frames.closed == 1 and len(frames.frames) == 1


async def test_control_spot_run_path_and_limits() -> None:
    clock = ManualClock()
    plan = plan_of([(3.0, 6.0), (6.0, 6.0)], [(6.0, 7.0), (3.0, 7.0)])
    run = make_run(Scripted(clock, [(3.0, 6.0, 0.0)]), FakeFrames(), plan, clock)
    path = run.mow_path()
    assert path.points_total == 4 and path.kinds == ["mow", "mow"]
    assert path.progress is not None and path.progress.index == 0
    assert run.max_run_s == pytest.approx(2 * (3 + 1 + 3) / 0.2 + 60)
    assert run.correlation_id == f"spot:{run.run_id}"
    assert run.status.segments_total == 2 and run.status.dry_run
    with pytest.raises(ValueError, match="speed_mps"):
        SpotMowRun(
            run_id=uuid4(),
            plan=plan,
            mower_map=MAP,
            pose_source=lambda: None,
            frames=FakeFrames(),
            speed_mps=0.5,
        )


def test_control_spot_run_geofence() -> None:
    fence = Geofence.of(MAP, 0.3)
    assert fence.contains((1.0, 1.0))
    assert fence.violation((0.2, 1.0)) is not None  # within the margin of the edge
    assert fence.violation((8.8, 10.0)) is not None  # within the margin of the pond
    assert fence.violation((10.0, 10.0)) is not None  # inside the pond
    assert fence.violation((20.0, 1.0)) is not None  # outside
    assert fence.segment_clear((1.0, 1.0), (8.0, 1.0))
    assert not fence.segment_clear((8.0, 10.0), (12.0, 10.0))


# --- a stale state at the start (cloud-first mowers) ----------------------------------------


def test_control_spot_start_accepts_a_cloud_first_state_age() -> None:
    """A standing mower on a cloud connection reports about every 20 s: the start allows a
    state up to a minute old (the run waits for live positions itself)."""
    now = datetime(2026, 6, 1, 12, 0, 0, tzinfo=UTC)
    assert spot_start_problem(state_at(3.0, 6.0, 0.0, now - timedelta(seconds=20)), now=now) is None
    assert spot_start_problem(state_at(3.0, 6.0, 0.0, now - timedelta(seconds=60)), now=now) is None
    old = spot_start_problem(state_at(3.0, 6.0, 0.0, now - timedelta(seconds=61)), now=now)
    assert old is not None and "61 s old" in old
    assert SPOT_START_MAX_STATE_AGE_S == 60.0


async def test_control_spot_run_waits_for_a_fresh_pose_when_the_first_is_stale() -> None:
    """The first pose is 20 s old: the run pauses (``position_stale``) without moving and drives
    once live poses arrive; it does not abort."""
    clock = ManualClock()
    frames = FakeFrames()
    script = Scripted(clock, [(3.0 + 0.2 * i, 6.0, 0.0) for i in range(40)])
    clock.advance(20)
    statuses: list[SpotRunStatus] = []
    run = SpotMowRun(
        run_id=uuid4(),
        plan=plan_of([(3.0, 6.0), (6.0, 6.0)]),
        mower_map=MAP,
        pose_source=script,
        frames=frames,
        speed_mps=0.2,
        on_status=statuses.append,
        clock=clock,
        sleep=script.sleep,
    )
    assert run.prepare(script()) is None
    report = await run.run()
    seen = [(s.state, s.reason) for s in statuses]
    paused = seen.index(("paused_rtk", "position_stale"))
    assert all(state == "spot_mowing" for state, _ in seen[:paused]), seen
    assert frames.frames[0] == (0.0, 0.0, False), "no movement while the pose is stale"
    assert report.state == "done", report.summary()
    assert 0 < report.paused_s < 5


# --- undock, transit and return (ADR 0041) ------------------------------------------------------


GARDEN = default_map()
"""Front lawn, back lawn behind it, the side path between both, the dock left of the front."""


async def test_control_spot_run_full_cycle_from_the_dock() -> None:
    h = Harness(mower_map=GARDEN)
    await h.start(idle=False)
    assert h.sim.snapshot().activity in (MowerActivity.DOCKED, MowerActivity.CHARGING)
    line = spiral((5.0, 14.0), r1=1.0)
    result, report = await h.run(SpotMow(plan=plan_of(line), speed_mps=0.2))
    assert result.status is CommandStatus.ACCEPTED, result.reason
    assert "leaving the dock" in (result.reason or "") and "transit" in (result.reason or "")
    assert report is not None
    assert report.state == "done", report.summary()
    assert report.undocked and report.sent_home and report.transit_m > 10
    assert report.dry_run and not h.blade_seen, "blades ran during the cycle"
    order = ["undocking", "waiting_rtk", "transit", "spot_mowing", "returning", "done"]
    assert [s for s in h.run_states if s in order] == order, h.run_states
    # the true track stayed in the drivable space (transit) and the spot's geofence
    space = DrivableSpace.of(GARDEN)
    outside = [p for p in h.track if not space.contains(p)]
    assert not outside, f"left the drivable space at {outside[:3]}"
    assert not h.controller.spot_run_active and not h.controller.drive_session_active
    # the mower drives home by itself
    await h.until(
        lambda: (
            h.sim.snapshot().activity
            in (MowerActivity.RETURNING, MowerActivity.DOCKED, MowerActivity.CHARGING)
        ),
        5,
        "the mower was not sent home",
    )
    await h.close()


async def test_control_spot_run_publishes_the_transit_as_a_transfer_path() -> None:
    h = Harness(mower_map=GARDEN)
    await h.start(idle=False)
    started = await h.send(SpotMow(plan=plan_of(spiral((5.0, 14.0), r1=1.0))))
    assert started.status is CommandStatus.ACCEPTED, started.reason
    path = h.controller.current_path()
    assert path is not None and path.kinds is not None and GARDEN.dock is not None
    assert path.kinds[0] == "transfer" and path.kinds[1] == "mow"
    assert path.polylines[0][0] == pytest.approx((GARDEN.dock.x, GARDEN.dock.y))
    stopped = await h.send(SpotStop())
    assert stopped.status is CommandStatus.EXECUTED
    report = await h.controller.spot_run_result(started.command_id)
    assert report is not None and report.reason == REASON_STOPPED_BY_USER
    assert not report.sent_home, "a stop must not send the mower home"
    await h.close()


async def test_control_spot_run_aborts_when_rtk_never_fixes_after_undocking() -> None:
    h = Harness(mower_map=GARDEN, spot_config=SpotRunConfig(undock_timeout_s=20.0))
    await h.start(idle=False)
    h.sim.set_rtk(RtkFixStatus.FLOAT)
    await h.settle()
    result, report = await h.run(SpotMow(plan=plan_of(spiral((5.0, 14.0), r1=1.0))))
    assert result.status is CommandStatus.ACCEPTED, result.reason
    assert report is not None
    assert (report.state, report.reason) == ("aborted", REASON_RTK_LOST), report.summary()
    assert report.undocked and report.frames_sent == 0, "frames went out without RTK"
    assert "waiting_rtk" in h.run_states
    assert report.sent_home, "a healthy mower is sent home after an abort"
    assert not h.blade_seen and not h.controller.drive_session_active
    await h.close()


async def test_control_spot_run_without_a_route_is_rejected_before_it_moves() -> None:
    far = GARDEN.model_copy(update={"paths": []})  # the back lawn has no connection any more
    h = Harness(mower_map=far)
    await h.start(idle=False)
    refused = await h.send(SpotMow(plan=plan_of(spiral((5.0, 14.0), r1=1.0))))
    assert refused.status is CommandStatus.REJECTED
    assert (refused.reason or "").startswith(f"{REASON_NO_ROUTE}:"), refused.reason
    assert h.sim.snapshot().activity in (MowerActivity.DOCKED, MowerActivity.CHARGING)
    assert not h.controller.spot_run_active
    await h.close()


async def test_control_spot_run_far_transit_from_the_lawn() -> None:
    """No 10 m limit any more: from the front lawn's far corner to the back lawn."""
    h = Harness(mower_map=GARDEN)
    await h.start()
    assert h.sim.snapshot().activity is MowerActivity.IDLE
    result, report = await h.run(
        SpotMow(plan=plan_of(spiral((5.0, 14.0), r1=0.8)), return_to_dock=False)
    )
    assert result.status is CommandStatus.ACCEPTED, result.reason
    assert report is not None
    assert report.state == "done", report.summary()
    assert not report.undocked and not report.sent_home and report.transit_m > 10
    await h.close()


async def test_control_spot_run_closing_the_controller_never_sends_the_mower_home() -> None:
    h = Harness(mower_map=GARDEN)
    await h.start()
    started = await h.send(SpotMow(plan=plan_of(spiral((5.0, 14.0), r1=0.8))))
    assert started.status is CommandStatus.ACCEPTED, started.reason
    await h.until(lambda: h.controller.drive_session_active, 10, "the run never drove")
    waiter = asyncio.ensure_future(h.controller.spot_run_result(started.command_id))
    await h.close()
    report = await waiter
    assert report is not None and report.reason == REASON_LINK_LOST
    assert not report.sent_home and report.return_problem is None
    assert h.sim.snapshot().activity is not MowerActivity.RETURNING
