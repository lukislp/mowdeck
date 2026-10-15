"""Spot mowing driven by MowDeck (ADR 0039): :class:`SpotMowRun` steers a mower along a Planner
route with joystick frames.

The mower runs no job of its own. The run reads the mower's generic state (position, heading,
RTK fix, ``observed_at``), keeps a geofence built from the mower's map and sends
``ManualDrive`` frames through a :class:`FrameChannel` (the controller, so the dead-man switch,
the blade rules and the sensor-stop handling apply unchanged). Nothing here knows a
manufacturer: a second adapter with a drive session and a state with position and heading runs
spot mowing without changes.

Control loop (5 Hz frames, ``ttl_ms`` 1000):

* **Pose**: the latest state is converted to the map's local frame (metres, radians
  counter-clockwise from +x). Between reports the pose is dead-reckoned from the commanded
  speed and an assumed yaw rate; each report replaces it.
* **Geofence**: the mapped areas inset by ``geofence_margin_m``, minus the no-go zones and
  obstacles buffered by the same margin. Every plan point is checked before the first frame,
  the pose on every report and on every dead-reckoned estimate; a violation aborts.
* **Pure pursuit**: the target is the point on the current leg ``lookahead_m`` ahead of the
  mower's projection (never past the leg's end). A heading error above ``turn_in_place_deg``
  turns in place (``angular = clamp(turn_gain * error, ±turn_max)``, left positive); otherwise
  ``angular = clamp(steer_gain * error, ±steer_max)`` and a speed governor adjusts ``linear``
  (within ``min_linear`` .. ``max_linear``) so the measured ground speed meets the segment
  speed (the stick-to-speed scale of a mower is unknown). A waypoint counts as reached within
  ``waypoint_radius_m``.
* **Approach**: a straight leg from the current pose to the first plan point is prepended when
  it is shorter than ``approach_max_m`` and clear within the geofence; otherwise the run is
  refused before it starts.
* **Pauses**: a pose older than ``max_pose_age_s`` or an RTK fix other than ``fixed`` pauses the
  run (``paused_rtk``, zero frames at ``pause_frame_hz`` keep the session open); after
  ``rtk_wait_s`` without a usable pose the run aborts ``rtk_lost``.
* **Aborts**: a cross-track error above ``max_cross_track_m``, or a heading error that grows for
  ``sign_error_reports`` consecutive reports while turning in place (a turn-direction sign
  error), end the run ``heading_mismatch``. A stop request (user, emergency stop, sensor stop,
  activity change, the link), a refused frame and the run-time limit
  ``min(2 x estimated + 60 s, 20 min)`` end it as well. Every end sends a zero frame, switches
  the blades off and closes the drive session (:meth:`FrameChannel.close`).
* **Blades**: on only for ``mow`` and ``turn`` segments that ask for them and only when the
  run was started with blades allowed; otherwise the whole run is a ``dry_run``.

The run publishes :class:`~mowdeck_contracts.v1.SpotRunStatus` through its status callback
(the controller merges it into ``MowerState.spot_run``) and ends with a :class:`SpotRunReport`.
"""

import asyncio
import contextlib
import logging
import math
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Final, Protocol
from uuid import UUID

from mowdeck_contracts.v1 import (
    MAX_SPOT_MOW_SPEED_MPS,
    CommandStatus,
    LocalPoint,
    MowerMap,
    MowerState,
    MowPath,
    MowPathKind,
    MowPathProgress,
    RtkFixStatus,
    SpotMowPlan,
    SpotRunState,
    SpotRunStatus,
    utc_now,
)

from mowdeck_control.geo import (
    boundary_distance,
    distance,
    from_wgs84,
    local_angle,
    point_in_polygon,
    segment_distance,
    wrap_angle,
)

_log = logging.getLogger("mowdeck_control.spot_run")

# --- reasons (``SpotRunStatus.reason`` and ``SpotRunReport.reason``) ----------------------------

REASON_RTK_LOST: Final = "rtk_lost"
REASON_GEOFENCE: Final = "geofence"
REASON_LINK_LOST: Final = "link_lost"
REASON_STOPPED_BY_USER: Final = "stopped_by_user"
REASON_EMERGENCY_STOP: Final = "emergency_stop"
REASON_SENSOR_STOP: Final = "sensor_stop"
REASON_TIMEOUT: Final = "timeout"
REASON_HEADING_MISMATCH: Final = "heading_mismatch"
REASON_ACTIVITY_CHANGE: Final = "activity_change"
REASON_FRAME_REJECTED: Final = "frame_rejected"
PAUSE_POSITION_STALE: Final = "position_stale"
"""``SpotRunStatus.reason`` while paused because no fresh pose arrived."""

MAX_RUN_S: Final = 20 * 60.0
"""Longest spot run, whatever the plan estimates."""
RUN_TIME_SLACK_S: Final = 60.0
"""Added to twice the estimated duration for the run-time limit."""

_PATH_KINDS: Final[dict[str, MowPathKind]] = {"mow": "mow", "transfer": "transfer", "turn": "other"}


# --- configuration -----------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SpotRunConfig:
    """Tunable behaviour of a :class:`SpotMowRun` (the defaults are the design values)."""

    frame_hz: float = 5.0
    """Drive frames per second while moving."""
    ttl_ms: int = 1000
    """Dead-man time of every frame."""
    pause_frame_hz: float = 2.0
    """Zero frames per second while paused (keeps the drive session open)."""
    lookahead_m: float = 0.7
    """Pure pursuit lookahead along the current leg."""
    turn_in_place_deg: float = 35.0
    """Heading error above which the mower turns in place."""
    turn_gain: float = 0.6
    turn_max: float = 0.4
    steer_gain: float = 0.8
    steer_max: float = 0.3
    waypoint_radius_m: float = 0.25
    """A waypoint is reached within this distance."""
    max_cross_track_m: float = 1.0
    """Distance from the current leg above which the run aborts (``heading_mismatch``)."""
    max_pose_age_s: float = 2.5
    """A pose older than this pauses the run."""
    rtk_wait_s: float = 30.0
    """A pause longer than this aborts the run (``rtk_lost``)."""
    approach_max_m: float = 10.0
    """Longest approach leg from the mower to the first plan point."""
    geofence_margin_m: float = 0.3
    max_linear: float = 0.5
    """Highest ``linear`` stick value the governor may command."""
    min_linear: float = 0.2
    """Lowest ``linear`` stick value while moving (above the adapters' dead zones)."""
    assumed_full_speed_mps: float = 0.5
    """Ground speed assumed for ``linear = 1`` (the governor's starting point)."""
    assumed_yaw_rate_rad_s: float = 1.0
    """Yaw rate assumed for ``angular = 1`` (dead reckoning between reports)."""
    governor_gain: float = 0.5
    """How strongly a measured speed error moves the stick (per report)."""
    sign_error_reports: int = 3
    """Consecutive reports with a growing heading error while turning that abort the run."""
    sign_error_min_deg: float = 2.0
    """The error must grow by at least this much per report to count."""


# --- geometry ---------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Pose:
    """The mower in the map's local frame."""

    x: float
    y: float
    theta: float
    """Heading in radians, counter-clockwise from +x."""
    observed_at: datetime
    rtk_fix: RtkFixStatus

    @property
    def point(self) -> LocalPoint:
        """``(x, y)``."""
        return (self.x, self.y)


def pose_of(state: MowerState | None, mower_map: MowerMap) -> Pose | None:
    """The pose of ``state`` in the map frame; None without a position or heading."""
    if state is None or state.position is None or state.position.heading is None:
        return None
    x, y = from_wgs84(mower_map, state.position.lat, state.position.lon)
    theta = local_angle(mower_map, state.position.heading)
    return Pose(x, y, theta, state.observed_at, state.rtk_fix)


@dataclass(frozen=True, slots=True)
class Geofence:
    """Where a spot run may drive: the mapped areas inset by ``margin_m``, minus every no-go
    zone and obstacle buffered by ``margin_m``."""

    areas: tuple[tuple[LocalPoint, ...], ...]
    zones: tuple[tuple[LocalPoint, ...], ...]
    margin_m: float = 0.3

    @classmethod
    def of(cls, mower_map: MowerMap, margin_m: float = 0.3) -> "Geofence":
        """The geofence of ``mower_map``."""
        return cls(
            areas=tuple(tuple(area.boundary) for area in mower_map.areas),
            zones=tuple(tuple(zone.boundary) for zone in mower_map.forbidden_zones()),
            margin_m=margin_m,
        )

    def violation(self, point: LocalPoint) -> str | None:
        """None when ``point`` lies inside the fence, else what is wrong."""
        for zone in self.zones:
            if point_in_polygon(point, zone) or boundary_distance(point, zone) < self.margin_m:
                return f"within {self.margin_m:g} m of a no-go zone or obstacle"
        for area in self.areas:
            if point_in_polygon(point, area) and boundary_distance(point, area) >= self.margin_m:
                return None
        return f"outside the mapped areas or within {self.margin_m:g} m of their edge"

    def contains(self, point: LocalPoint) -> bool:
        """True when ``point`` lies inside the fence."""
        return self.violation(point) is None

    def segment_clear(self, start: LocalPoint, end: LocalPoint, *, step_m: float = 0.1) -> bool:
        """True when every sample of the straight segment lies inside the fence."""
        steps = max(1, math.ceil(distance(start, end) / step_m))
        dx, dy = end[0] - start[0], end[1] - start[1]
        return all(
            self.contains((start[0] + dx * i / steps, start[1] + dy * i / steps))
            for i in range(steps + 1)
        )


# --- frames -------------------------------------------------------------------------------------


class FrameChannel(Protocol):
    """Where a run sends its drive frames (the controller's ``ManualDrive`` path)."""

    async def send(
        self, *, linear: float, angular: float, blade_on: bool, ttl_ms: int
    ) -> str | None:
        """Send one frame; None when it was accepted, else why it was refused (ends the run).

        A refusal that starts with ``failed`` (the link) ends the run ``link_lost``, one that
        names a sensor stop ends it ``sensor_stop``, any other ``frame_rejected``.
        """
        ...

    async def close(self) -> None:
        """Zero frame, blades off and release the drive session; never raises."""
        ...


# --- results ------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SpotRunReport:
    """How a spot run ended."""

    run_id: UUID
    state: SpotRunState
    reason: str | None
    detail: str | None
    planned_points: int
    driven_points: int
    duration_s: float
    coverage_percent: float
    """Share of the plan's mowing waypoints reached (an estimate of the covered part)."""
    dry_run: bool
    frames_sent: int
    paused_s: float

    @property
    def command_status(self) -> CommandStatus:
        """The final status of the ``spot_mow`` command: ``executed`` for a run that drove the
        whole plan, ``failed`` otherwise (``reason`` says why)."""
        return CommandStatus.EXECUTED if self.state == "done" else CommandStatus.FAILED

    def summary(self) -> str:
        """One line for the command result and the log."""
        head = f"spot run {self.state}"
        if self.reason:
            head += f": {self.reason}"
        if self.detail:
            head += f" ({self.detail})"
        return (
            f"{head}; {self.driven_points}/{self.planned_points} points, "
            f"{self.coverage_percent:.0f} % coverage, {self.duration_s:.0f} s"
            + (", dry run" if self.dry_run else ", blades used")
        )


# --- the run ------------------------------------------------------------------------------------


@dataclass(slots=True)
class _Segment:
    points: list[LocalPoint]
    kind: str
    blades_on: bool
    speed_mps: float
    planned: bool = True
    """False for the approach leg (its points do not count as plan points)."""


@dataclass(slots=True)
class _Progress:
    segment: int = 0
    target: int = 1
    """Index of the waypoint being driven to within the segment (0 is the segment start)."""


StatusCallback = Callable[[SpotRunStatus], None]
PoseSource = Callable[[], MowerState | None]
Sleep = Callable[[float], Awaitable[None]]


class SpotMowRun:
    """One spot run: drives ``plan`` from the mower's current pose (see the module docs)."""

    def __init__(
        self,
        *,
        run_id: UUID,
        plan: SpotMowPlan,
        mower_map: MowerMap,
        pose_source: PoseSource,
        frames: FrameChannel,
        speed_mps: float = 0.2,
        blades: bool = False,
        on_status: StatusCallback | None = None,
        clock: Callable[[], datetime] = utc_now,
        sleep: Sleep | None = None,
        config: SpotRunConfig | None = None,
    ) -> None:
        if not 0 < speed_mps <= MAX_SPOT_MOW_SPEED_MPS:
            msg = f"speed_mps must be within (0, {MAX_SPOT_MOW_SPEED_MPS}]"
            raise ValueError(msg)
        self.run_id = run_id
        self.plan = plan
        self._map = mower_map
        self._pose_source = pose_source
        self._frames = frames
        self._blades = blades
        self._on_status = on_status
        self._clock = clock
        self._sleep: Sleep = sleep or self._wait
        self._config = config or SpotRunConfig()
        self._fence = Geofence.of(mower_map, self._config.geofence_margin_m)
        self._segments = _segments_of(plan, speed_mps, self._config.waypoint_radius_m)
        self._planned_points = sum(len(s.points) for s in self._segments if s.planned)
        self._mow_points = sum(
            len(s.points) for s in self._segments if s.planned and s.kind == "mow"
        )
        self._planned_at = clock()
        self._progress = _Progress()
        self._state: SpotRunState = "preparing"
        self._reason: str | None = None
        self._detail: str | None = None
        self._driven = 0
        self._mow_driven = 0
        self._frames_sent = 0
        self._paused_s = 0.0
        self._started_at: datetime | None = None
        self._ended_at: datetime | None = None
        self._stop_reason: str | None = None
        self._stop_detail: str | None = None
        self._finished = asyncio.Event()
        self._wake = asyncio.Event()
        self._report: SpotRunReport | None = None
        self._prepared = False
        # control state
        self._last_report: Pose | None = None
        self._previous_report: Pose | None = None
        self._last_cmd: tuple[float, float] = (0.0, 0.0)
        self._cmd_at: datetime | None = None
        self._moving_since: datetime | None = None
        self._linear = self._clamp(
            speed_mps / self._config.assumed_full_speed_mps,
            self._config.min_linear,
            self._config.max_linear,
        )
        self._measured_speed: float | None = None
        self._turning = False
        self._turn_errors: list[float] = []

    # --- properties ----------------------------------------------------------------------------

    @property
    def correlation_id(self) -> str:
        """Correlation id of the run's frames: ``spot:<run_id>``."""
        return f"spot:{self.run_id}"

    @property
    def dry_run(self) -> bool:
        """True when the blades stay off for the whole run."""
        return not self._blades

    @property
    def state(self) -> SpotRunState:
        """The current life-cycle state."""
        return self._state

    @property
    def finished(self) -> bool:
        """True once the run ended (``done``, ``aborted`` or ``failed``)."""
        return self._finished.is_set()

    @property
    def active(self) -> bool:
        """True from :meth:`prepare` until the run ended."""
        return self._prepared and not self.finished

    @property
    def report(self) -> SpotRunReport | None:
        """The final report; None while the run goes on."""
        return self._report

    @property
    def status(self) -> SpotRunStatus:
        """The status as published in ``MowerState.spot_run``."""
        planned = self._planned_points
        percent = round(100 * self._driven / planned) if planned else 0
        return SpotRunStatus(
            run_id=self.run_id,
            state=self._state,
            progress_percent=max(0, min(100, percent)),
            segment_index=min(self._planned_index(), max(0, len(self.plan.segments) - 1)),
            segments_total=len(self.plan.segments),
            planned_points=planned,
            driven_points=self._driven,
            dry_run=self.dry_run,
            reason=self._reason,
        )

    def mow_path(self) -> MowPath:
        """The plan as a :class:`~mowdeck_contracts.v1.MowPath` with the progress (for the
        gateway's path snapshot, so clients draw it with the mow-path layer)."""
        planned = [s for s in self._segments if s.planned]
        return MowPath(
            path_hash=self.plan.plan_hash[:32],
            polylines=[list(s.points) for s in planned],
            kinds=[_PATH_KINDS.get(s.kind, "other") for s in planned],
            points_total=self._planned_points,
            progress=MowPathProgress(index=self._driven),
            planned_at=self._planned_at,
        )

    @property
    def max_run_s(self) -> float:
        """The run-time limit: ``min(2 x estimated + 60 s, 20 min)``."""
        estimated = self.plan.estimated_duration_s
        if estimated <= 0:
            estimated = sum(
                distance(a, b) / max(s.speed_mps, 0.05)
                for s in self._segments
                for a, b in zip(s.points, s.points[1:], strict=False)
            )
        return min(2 * estimated + RUN_TIME_SLACK_S, MAX_RUN_S)

    # --- pre-flight ------------------------------------------------------------------------------

    def prepare(self, state: MowerState | None) -> str | None:
        """Check the plan and the mower's pose before the first frame; None when the run may
        start, else why not (the controller rejects the command with it).

        Prepends the approach leg when the mower does not stand at the first plan point.
        """
        for segment in self._segments:
            for point in segment.points:
                problem = self._fence.violation(point)
                if problem is not None:
                    where = f"({point[0]:.2f}, {point[1]:.2f})"
                    return f"the plan leaves the geofence at {where}: {problem}"
        pose = pose_of(state, self._map)
        if pose is None:
            return "the mower reports no position or heading"
        if pose.rtk_fix is not RtkFixStatus.FIXED:
            return f"no fixed RTK position (the fix is {pose.rtk_fix.value})"
        problem = self._fence.violation(pose.point)
        if problem is not None:
            return f"the mower stands {problem}"
        first = self._segments[0].points[0]
        gap = distance(pose.point, first)
        if gap > self._config.approach_max_m:
            return (
                f"the mower is {gap:.1f} m from the start of the plan "
                f"(at most {self._config.approach_max_m:g} m)"
            )
        if gap > self._config.waypoint_radius_m:
            if not self._fence.segment_clear(pose.point, first):
                return "no clear straight approach from the mower to the start of the plan"
            self._segments.insert(
                0,
                _Segment(
                    points=[pose.point, first],
                    kind="transfer",
                    blades_on=False,
                    speed_mps=self._segments[0].speed_mps,
                    planned=False,
                ),
            )
        self._prepared = True
        return None

    # --- control ---------------------------------------------------------------------------------

    def request_stop(self, reason: str, detail: str | None = None) -> None:
        """Ask the run to end with ``reason`` (``stopped_by_user``, ``emergency_stop``, ...);
        the next loop iteration sends the zero frame and closes."""
        if self.finished or self._stop_reason is not None:
            return
        self._stop_reason = reason
        self._stop_detail = detail
        if self._started_at is not None:
            self._set_state("stopping", reason)
        self._wake.set()

    async def wait(self) -> SpotRunReport:
        """Wait until the run ended and return its report."""
        await self._finished.wait()
        assert self._report is not None  # noqa: S101 - set before the event
        return self._report

    async def run(self) -> SpotRunReport:
        """Drive the plan; returns when the run ended (see the module docs)."""
        if not self._prepared:
            msg = "prepare() must succeed before run()"
            raise RuntimeError(msg)
        self._started_at = self._clock()
        self._set_state(self._driving_state(), None)
        self._notify()
        try:
            await self._loop()
        except asyncio.CancelledError:
            self._end("aborted", REASON_LINK_LOST, "the controller closed")
            await self._frames.close()
            self._finish()
            raise
        except Exception as exc:  # the run must always leave a report behind
            _log.exception("spot run %s failed", self.run_id)
            self._end("failed", "internal_error", type(exc).__name__)
        await self._frames.close()
        self._finish()
        assert self._report is not None  # noqa: S101 - set by _finish
        return self._report

    async def _loop(self) -> None:
        config = self._config
        paused_since: datetime | None = None
        while True:
            now = self._clock()
            if self._stop_reason is not None:
                self._end("aborted", self._stop_reason, self._stop_detail)
                return
            assert self._started_at is not None  # noqa: S101 - set in run()
            if (now - self._started_at).total_seconds() > self.max_run_s:
                self._end("aborted", REASON_TIMEOUT, f"longer than {self.max_run_s:.0f} s")
                return
            pose = pose_of(self._pose_source(), self._map)
            usable = pose is not None and self._usable(pose, now)
            if not usable:
                if paused_since is None:
                    paused_since = now
                    self._last_cmd = (0.0, 0.0)
                    self._moving_since = None
                    reason = PAUSE_POSITION_STALE if pose is None else self._pause_reason(pose, now)
                    self._set_state("paused_rtk", reason)
                elif (now - paused_since).total_seconds() > config.rtk_wait_s:
                    self._paused_s += (now - paused_since).total_seconds()
                    self._end("aborted", REASON_RTK_LOST, self._reason)
                    return
                if not await self._send(0.0, 0.0, blade_on=False):
                    return
                await self._sleep(1 / config.pause_frame_hz)
                continue
            assert pose is not None  # noqa: S101 - usable
            if paused_since is not None:
                self._paused_s += (now - paused_since).total_seconds()
                paused_since = None
                self._set_state(self._driving_state(), None)
            new_report = (
                self._last_report is None or pose.observed_at != self._last_report.observed_at
            )
            if new_report and not self._on_report(pose):
                return
            estimate = self._estimate(now)
            violation = self._fence.violation(estimate)
            if violation is not None:
                self._end("aborted", REASON_GEOFENCE, violation)
                return
            if not self._advance(estimate):
                self._end("done", None, None)
                return
            linear, angular = self._steer(estimate, now)
            segment = self._segments[self._progress.segment]
            blade_on = self._blades and segment.blades_on
            if not await self._send(linear, angular, blade_on=blade_on):
                return
            self._last_cmd = (linear, angular)
            self._cmd_at = now
            if linear > 0:
                self._moving_since = self._moving_since or now
            else:
                self._moving_since = None
            await self._sleep(1 / config.frame_hz)

    def _usable(self, pose: Pose, now: datetime) -> bool:
        age = (now - pose.observed_at).total_seconds()
        return pose.rtk_fix is RtkFixStatus.FIXED and age <= self._config.max_pose_age_s

    def _pause_reason(self, pose: Pose, now: datetime) -> str:
        if pose.rtk_fix is not RtkFixStatus.FIXED:
            return f"rtk_{pose.rtk_fix.value}"
        return PAUSE_POSITION_STALE

    def _driving_state(self) -> SpotRunState:
        approach = self._progress.segment == 0 and not self._segments[0].planned
        return "preparing" if approach else "spot_mowing"

    async def _send(self, linear: float, angular: float, *, blade_on: bool) -> bool:
        """Send one frame; False (and the run ended) when it was refused."""
        refusal = await self._frames.send(
            linear=linear, angular=angular, blade_on=blade_on, ttl_ms=self._config.ttl_ms
        )
        self._frames_sent += 1
        if refusal is None:
            return True
        lowered = refusal.casefold()
        if "sensor stop" in lowered:
            reason = REASON_SENSOR_STOP
        elif lowered.startswith("failed") or "not connected" in lowered:
            reason = REASON_LINK_LOST
        else:
            reason = REASON_FRAME_REJECTED
        self._end("aborted", reason, refusal)
        return False

    # --- reports -------------------------------------------------------------------------------

    def _on_report(self, pose: Pose) -> bool:
        """Take a new report: speed measurement, sign-error and cross-track checks. False when
        the run ended."""
        previous = self._last_report
        self._previous_report, self._last_report = previous, pose
        violation = self._fence.violation(pose.point)
        if violation is not None:
            self._end("aborted", REASON_GEOFENCE, f"reported position {violation}")
            return False
        if previous is not None:
            dt = (pose.observed_at - previous.observed_at).total_seconds()
            moving = (
                self._moving_since is not None
                and self._moving_since <= previous.observed_at
                and self._last_cmd[0] > 0
            )
            if moving and 0.2 <= dt <= 3.0:
                self._measured_speed = distance(previous.point, pose.point) / dt
                self._govern()
        if self._turning:
            error = abs(self._heading_error(pose.x, pose.y, pose.theta))
            errors = self._turn_errors
            if errors and error > errors[-1] + math.radians(self._config.sign_error_min_deg):
                errors.append(error)
            else:
                errors.clear()
                errors.append(error)
            if len(errors) > self._config.sign_error_reports:
                self._end(
                    "aborted",
                    REASON_HEADING_MISMATCH,
                    f"the heading error grew for {self._config.sign_error_reports} reports "
                    "while turning (turn direction?)",
                )
                return False
        cross = self._cross_track(pose.point)
        if cross > self._config.max_cross_track_m:
            self._end("aborted", REASON_HEADING_MISMATCH, f"cross-track error {cross:.2f} m")
            return False
        return True

    def _govern(self) -> None:
        measured = self._measured_speed
        if measured is None:
            return
        config = self._config
        target = self._segments[self._progress.segment].speed_mps
        step = config.governor_gain * (target - measured) / config.assumed_full_speed_mps
        self._linear = self._clamp(self._linear + step, config.min_linear, config.max_linear)

    # --- geometry of the plan -------------------------------------------------------------------

    def _leg(self) -> tuple[LocalPoint, LocalPoint]:
        segment = self._segments[self._progress.segment]
        return segment.points[self._progress.target - 1], segment.points[self._progress.target]

    def _cross_track(self, point: LocalPoint) -> float:
        start, end = self._leg()
        return segment_distance(point, start, end)

    def _advance(self, estimate: LocalPoint) -> bool:
        """Move the target past every reached waypoint; False when the plan is complete."""
        radius = self._config.waypoint_radius_m
        while True:
            segment = self._segments[self._progress.segment]
            target = segment.points[self._progress.target]
            start = segment.points[self._progress.target - 1]
            reached = distance(estimate, target) <= radius or (
                _projection(estimate, start, target) >= 1.0
                and distance(estimate, target) <= 2 * radius
            )
            if not reached:
                return True
            self._turn_errors.clear()
            if segment.planned:
                if self._progress.target == 1:
                    self._count_point(segment)  # the segment's start point
                self._count_point(segment)
            if self._progress.target + 1 < len(segment.points):
                self._progress.target += 1
            else:
                if self._progress.segment + 1 >= len(self._segments):
                    self._notify()
                    return False
                self._progress.segment += 1
                self._progress.target = 1
                self._measured_speed = None
                self._set_state(self._driving_state(), None)
            self._notify()

    def _count_point(self, segment: _Segment) -> None:
        self._driven += 1
        if segment.kind == "mow":
            self._mow_driven += 1

    def _planned_index(self) -> int:
        """Index of the current segment within the plan (the approach leg does not count)."""
        return sum(1 for s in self._segments[: self._progress.segment] if s.planned)

    def _lookahead(self, estimate: LocalPoint) -> LocalPoint:
        start, end = self._leg()
        length = distance(start, end)
        if length < 1e-9:
            return end
        t = _projection(estimate, start, end)
        t = max(0.0, min(1.0, t + self._config.lookahead_m / length))
        return (start[0] + (end[0] - start[0]) * t, start[1] + (end[1] - start[1]) * t)

    def _heading_error(self, x: float, y: float, theta: float) -> float:
        target = self._lookahead((x, y))
        if distance((x, y), target) < 1e-6:
            return 0.0
        return wrap_angle(math.atan2(target[1] - y, target[0] - x) - theta)

    def _steer(self, estimate: LocalPoint, now: datetime) -> tuple[float, float]:
        theta = self._estimated_theta(now)
        error = self._heading_error(estimate[0], estimate[1], theta)
        config = self._config
        if abs(error) > math.radians(config.turn_in_place_deg):
            if not self._turning:
                self._turning = True
                self._turn_errors.clear()
            return 0.0, self._clamp(config.turn_gain * error, -config.turn_max, config.turn_max)
        self._turning = False
        self._turn_errors.clear()
        angular = self._clamp(config.steer_gain * error, -config.steer_max, config.steer_max)
        return self._linear, angular

    # --- dead reckoning ---------------------------------------------------------------------------

    def _estimate(self, now: datetime) -> LocalPoint:
        report = self._last_report
        assert report is not None  # noqa: S101 - a usable pose was seen
        dt = self._reckon_dt(report, now)
        linear, angular = self._last_cmd
        if dt <= 0 or linear <= 0:
            return report.point
        speed = (
            self._measured_speed
            if self._measured_speed is not None
            else (linear * self._config.assumed_full_speed_mps)
        )
        theta = report.theta + 0.5 * angular * self._config.assumed_yaw_rate_rad_s * dt
        return (report.x + math.cos(theta) * speed * dt, report.y + math.sin(theta) * speed * dt)

    def _estimated_theta(self, now: datetime) -> float:
        report = self._last_report
        assert report is not None  # noqa: S101 - a usable pose was seen
        dt = self._reckon_dt(report, now)
        return report.theta + self._last_cmd[1] * self._config.assumed_yaw_rate_rad_s * dt

    def _reckon_dt(self, report: Pose, now: datetime) -> float:
        since_report = (now - report.observed_at).total_seconds()
        since_cmd = (now - self._cmd_at).total_seconds() if self._cmd_at else 0.0
        return max(0.0, min(since_report, since_cmd, self._config.max_pose_age_s))

    # --- state -----------------------------------------------------------------------------------

    def _set_state(self, state: SpotRunState, reason: str | None) -> None:
        if (state, reason) == (self._state, self._reason):
            return
        self._state = state
        self._reason = reason
        _log.info("spot run %s: %s%s", self.run_id, state, f" ({reason})" if reason else "")
        self._notify()

    def _end(self, state: SpotRunState, reason: str | None, detail: str | None) -> None:
        if self._ended_at is not None:
            return
        self._ended_at = self._clock()
        self._detail = detail
        self._set_state(state, reason)
        if detail:
            _log.info("spot run %s: %s", self.run_id, detail)

    def _finish(self) -> None:
        if self._report is not None:
            return
        if self._ended_at is None:
            self._end("failed", "internal_error", "the run ended without a result")
        started = self._started_at or self._planned_at
        assert self._ended_at is not None  # noqa: S101 - set by _end
        coverage = 100.0 * self._mow_driven / self._mow_points if self._mow_points else 0.0
        self._report = SpotRunReport(
            run_id=self.run_id,
            state=self._state,
            reason=self._reason,
            detail=self._detail,
            planned_points=self._planned_points,
            driven_points=self._driven,
            duration_s=max(0.0, (self._ended_at - started).total_seconds()),
            coverage_percent=coverage,
            dry_run=self.dry_run,
            frames_sent=self._frames_sent,
            paused_s=self._paused_s,
        )
        self._finished.set()
        self._notify()

    def _notify(self) -> None:
        if self._on_status is None:
            return
        try:
            self._on_status(self.status)
        except Exception:
            _log.exception("spot run %s: status callback failed", self.run_id)

    async def _wait(self, seconds: float) -> None:
        """The default sleep: wakes early on a stop request."""
        with contextlib.suppress(TimeoutError):
            async with asyncio.timeout(seconds):
                await self._wake.wait()
        if self._stop_reason is not None:
            self._wake.set()

    @staticmethod
    def _clamp(value: float, low: float, high: float) -> float:
        return max(low, min(high, value))


def _segments_of(plan: SpotMowPlan, speed_mps: float, join_m: float) -> list[_Segment]:
    """The plan's segments with their speed capped at ``speed_mps``; a gap between the end of a
    segment and the start of the next (wider than ``join_m``) is bridged by a straight
    transfer leg without blades, so the cross-track check never sees a jump. Segments with a
    single point are reached through such a leg as well (they have no leg of their own)."""
    segments: list[_Segment] = []
    for segment in plan.segments:
        points = segment.points()
        speed = min(segment.speed_mps, speed_mps)
        previous = segments[-1].points[-1] if segments else None
        if previous is not None and distance(previous, points[0]) > join_m:
            segments.append(_Segment([previous, points[0]], "transfer", False, speed, False))
        if len(points) < 2:  # a leg needs two points
            if previous is None:
                points = [points[0], points[0]]
            else:
                continue
        segments.append(_Segment(points, segment.kind, segment.blades_on, speed))
    return segments


def _projection(p: LocalPoint, a: LocalPoint, b: LocalPoint) -> float:
    """Parameter ``t`` of the projection of ``p`` onto the line ``a``-``b`` (0 at ``a``, 1 at
    ``b``, unclamped)."""
    dx, dy = b[0] - a[0], b[1] - a[1]
    length = dx * dx + dy * dy
    if length == 0:
        return 1.0
    return ((p[0] - a[0]) * dx + (p[1] - a[1]) * dy) / length


def spot_start_problem(
    state: MowerState,
    *,
    now: datetime,
    max_state_age_s: float = 5.0,
    min_battery_percent: int = 20,
) -> str | None:
    """Why a spot run must not start from ``state``, or None: the mower stands idle (not docked,
    no job, no error), its state is fresh and the battery is above ``min_battery_percent``."""
    if state.activity.value != "idle":
        return f"the mower is {state.activity.value}; a spot run needs it idle on the lawn"
    age = (now - state.observed_at).total_seconds()
    if age > max_state_age_s:
        return f"the mower's state is {age:.0f} s old; wait for a fresh report"
    if state.battery_percent <= min_battery_percent:
        return f"battery {state.battery_percent} % is too low for a spot run"
    return None


__all__ = [
    "MAX_RUN_S",
    "PAUSE_POSITION_STALE",
    "REASON_ACTIVITY_CHANGE",
    "REASON_EMERGENCY_STOP",
    "REASON_FRAME_REJECTED",
    "REASON_GEOFENCE",
    "REASON_HEADING_MISMATCH",
    "REASON_LINK_LOST",
    "REASON_RTK_LOST",
    "REASON_SENSOR_STOP",
    "REASON_STOPPED_BY_USER",
    "REASON_TIMEOUT",
    "RUN_TIME_SLACK_S",
    "FrameChannel",
    "Geofence",
    "Pose",
    "SpotMowRun",
    "SpotRunConfig",
    "SpotRunReport",
    "pose_of",
    "spot_start_problem",
]
