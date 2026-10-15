"""Drive calibration (ADR 0042): :class:`DriveCalibrationRun` measures what the stick values
mean on the ground.

A spot run (ADR 0039) steers with normalised ``linear`` and ``angular`` values; how fast a mower
drives and turns at a given value depends on the model, the firmware and vendor settings. The
calibration drives a few short steps over the generic drive channel and reads only the neutral
state (position, heading, RTK fix, ``observed_at``):

* forward at ``linear`` 0.3, 0.5, 0.7 and 1.0, each ``step_s`` (default 2.5 s);
* turns in place at ``angular`` +0.3, -0.3, +0.6, -0.6, +1.0 and -1.0, each ``step_s``;
* zero frames for ``settle_s`` between two steps, so every step starts from standstill.

Per step it measures the ground speed (forward) or the yaw rate (turn) from the position
reports: between the first and the last report of the step when there are two, else from the
standing pose at the step's start to its only report. It then fits

``speed(c) = full_speed * (c - dead_zone) / (1 - dead_zone)`` for ``c > dead_zone``

by least squares over the forward steps (the same for the yaw rate over the turns, both
directions averaged) and returns a :class:`~mowdeck_contracts.v1.DriveCalibrationResult`.

Safety, the same for every manufacturer:

* blades off in every frame; the frames go through the controller (dead-man, sensor stops);
* before the first step the mower must stand idle out of its dock with a fresh, fixed RTK pose
  (zero frames keep the drive session open while it waits, at most ``start_wait_s``) and the
  straight line of ``room_ahead_m`` along its heading must lie inside the geofence (the mapped
  areas inset by the margin, minus the no-go zones);
* the forward steps stop once ``max_forward_m`` were covered (a step is shortened when the
  previous speed predicts it would pass the budget, and skipped when less than a second of
  driving would remain); every report is checked against the geofence;
* a stop request (``spot_stop``, emergency stop, sensor stop, a change of activity, the link)
  or a refused frame ends it; every end sends a zero frame and closes the drive session.
"""

import asyncio
import contextlib
import itertools
import logging
import math
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Final
from uuid import UUID

from mowdeck_contracts.v1 import (
    DriveCalibration,
    DriveCalibrationResult,
    DriveCalibrationState,
    DriveCalibrationStatus,
    DriveCalibrationStep,
    MowerActivity,
    MowerMap,
    MowerState,
    RtkFixStatus,
    utc_now,
)

from mowdeck_control.geo import distance, wrap_angle
from mowdeck_control.spot_run import (
    REASON_GEOFENCE,
    REASON_RTK_LOST,
    FrameChannel,
    Geofence,
    Pose,
    pose_of,
    refusal_reason,
)

_log = logging.getLogger("mowdeck_control.calibration")

REASON_NO_FIT: Final = "no_fit"
"""``failed``: the measurements do not allow a fit (``detail`` says why)."""
REASON_TURN_DIRECTION: Final = "turn_direction"
"""``failed``: the mower turned the other way than commanded (left must be positive)."""


@dataclass(frozen=True, slots=True)
class CalibrationConfig:
    """Tunable behaviour of a :class:`DriveCalibrationRun` (the defaults are the design
    values)."""

    forward_commands: tuple[float, ...] = (0.3, 0.5, 0.7, 1.0)
    turn_commands: tuple[float, ...] = (0.3, -0.3, 0.6, -0.6, 1.0, -1.0)
    step_s: float = 2.5
    """Duration of each step."""
    settle_s: float = 1.5
    """Zero frames between two steps."""
    frame_hz: float = 5.0
    ttl_ms: int = 1000
    max_forward_m: float = 1.5
    """Ground the forward steps may cover in total."""
    room_ahead_m: float = 2.0
    """Straight line along the heading that must lie inside the geofence before the start."""
    geofence_margin_m: float = 0.3
    max_pose_age_s: float = 2.5
    """A pose older than this does not count as fresh (before the first step)."""
    start_wait_s: float = 30.0
    """Longest wait for a fresh, fixed pose before the first step (``rtk_lost``)."""
    min_step_s: float = 1.0
    """A forward step shorter than this is skipped instead (too short to measure)."""
    min_speed_mps: float = 0.005
    """A step slower than this counts as standing still (below the dead zone)."""
    min_yaw_dps: float = 0.2
    """A turn slower than this counts as not turning."""


@dataclass(slots=True)
class _Report:
    at: datetime
    x: float
    y: float
    theta: float


StatusCallback = Callable[[DriveCalibrationStatus], None]
PoseSource = Callable[[], MowerState | None]
Sleep = Callable[[float], Awaitable[None]]


def room_ahead_problem(
    state: MowerState | None,
    mower_map: MowerMap,
    *,
    room_m: float = 2.0,
    margin_m: float = 0.3,
) -> str | None:
    """Why the calibration's forward steps do not fit in front of the mower, or None: the
    straight line of ``room_m`` along the heading must stay inside the geofence."""
    pose = pose_of(state, mower_map)
    if pose is None:
        return "the mower reports no position or heading"
    fence = Geofence.of(mower_map, margin_m)
    problem = fence.violation(pose.point)
    if problem is not None:
        return f"the mower stands {problem}"
    ahead = (pose.x + math.cos(pose.theta) * room_m, pose.y + math.sin(pose.theta) * room_m)
    if not fence.segment_clear(pose.point, ahead):
        return (
            f"not enough room ahead: the calibration needs {room_m:g} m of lawn straight in "
            "front of the mower"
        )
    return None


def calibration_start_problem(state: MowerState, *, now: datetime) -> str | None:
    """Why a calibration must not start from ``state`` (besides the room ahead), or None: an
    idle mower out of its dock, a state at most 60 s old, a fixed RTK position, enough
    battery."""
    if state.activity is not MowerActivity.IDLE:
        return f"the mower is {state.activity.value}; a calibration needs it idle out of its dock"
    age = (now - state.observed_at).total_seconds()
    if age > 60.0:
        return f"the mower's state is {age:.0f} s old; wait for a fresh report"
    if state.rtk_fix is not RtkFixStatus.FIXED:
        return f"no fixed RTK position (the fix is {state.rtk_fix.value})"
    if state.battery_percent <= 20:
        return f"battery {state.battery_percent} % is too low for a calibration"
    return None


class DriveCalibrationRun:
    """One calibration (see the module docs)."""

    def __init__(
        self,
        *,
        run_id: UUID,
        mower_map: MowerMap,
        pose_source: PoseSource,
        frames: FrameChannel,
        drive_speed_setting: Callable[[], float | None] | None = None,
        on_status: StatusCallback | None = None,
        clock: Callable[[], datetime] = utc_now,
        sleep: Sleep | None = None,
        config: CalibrationConfig | None = None,
    ) -> None:
        self.run_id = run_id
        self._map = mower_map
        self._pose_source = pose_source
        self._frames = frames
        self._setting = drive_speed_setting or (lambda: None)
        self._on_status = on_status
        self._clock = clock
        self._sleep: Sleep = sleep or self._wait
        self._config = config or CalibrationConfig()
        self._fence = Geofence.of(mower_map, self._config.geofence_margin_m)
        self._state: DriveCalibrationState = "preparing"
        self._reason: str | None = None
        self._detail: str | None = None
        self._steps: list[DriveCalibrationStep] = []
        self._step_index = 0
        self._result: DriveCalibrationResult | None = None
        self._started_at: datetime | None = None
        self._stop_reason: str | None = None
        self._stop_detail: str | None = None
        self._ended = False
        self._wake = asyncio.Event()
        self._finished = asyncio.Event()
        self._last: _Report | None = None
        self._frames_sent = 0

    # --- properties ------------------------------------------------------------------------

    @property
    def correlation_id(self) -> str:
        """Correlation id of the frames: ``calibration:<run_id>``."""
        return f"calibration:{self.run_id}"

    @property
    def steps_total(self) -> int:
        return len(self._config.forward_commands) + len(self._config.turn_commands)

    @property
    def active(self) -> bool:
        """True until the calibration ended."""
        return not self._finished.is_set()

    @property
    def finished(self) -> bool:
        return self._finished.is_set()

    @property
    def state(self) -> DriveCalibrationState:
        return self._state

    @property
    def reason(self) -> str | None:
        return self._reason

    @property
    def detail(self) -> str | None:
        return self._detail

    @property
    def result(self) -> DriveCalibrationResult | None:
        return self._result

    @property
    def status(self) -> DriveCalibrationStatus:
        """The status as published in ``MowerState.drive_calibration``."""
        reason = self._reason
        if self._detail and self._state in ("aborted", "failed"):
            reason = f"{reason}: {self._detail}" if reason else self._detail
        return DriveCalibrationStatus(
            run_id=self.run_id,
            state=self._state,
            step_index=min(self._step_index, max(0, self.steps_total - 1)),
            steps_total=self.steps_total,
            steps=list(self._steps),
            reason=reason[:200] if reason else None,
            result=self._result,
            started_at=self._started_at,
        )

    def summary(self) -> str:
        """One line for the command result and the log."""
        if self._state == "done" and self._result is not None:
            c = self._result.calibration
            return (
                f"calibration done: full speed {c.full_speed_mps:.3f} m/s, full yaw rate "
                f"{c.full_yaw_rate_dps:.1f} deg/s, dead zone {c.dead_zone:.2f} "
                f"(turns {c.turn_dead_zone:.2f})"
            )
        head = f"calibration {self._state}"
        if self._reason:
            head += f": {self._reason}"
        if self._detail:
            head += f" ({self._detail})"
        return head

    # --- control ---------------------------------------------------------------------------

    def request_stop(self, reason: str, detail: str | None = None) -> None:
        """End the calibration with ``reason`` at the next frame."""
        if self.finished or self._stop_reason is not None:
            return
        self._stop_reason = reason
        self._stop_detail = detail
        self._set_state("stopping", reason)
        self._wake.set()

    async def wait(self) -> DriveCalibrationStatus:
        """Wait for the end and return the final status."""
        await self._finished.wait()
        return self.status

    async def run(self) -> DriveCalibrationStatus:
        """Drive the steps and fit; returns the final status."""
        self._started_at = self._clock()
        self._notify()
        try:
            if await self._wait_for_pose():
                await self._measure_all()
        except asyncio.CancelledError:
            self._end("aborted", "link_lost", "the controller closed")
            await self._frames.close()
            self._finish()
            raise
        except Exception as exc:  # always leave a result behind
            _log.exception("calibration %s failed", self.run_id)
            self._end("failed", "internal_error", type(exc).__name__)
        await self._frames.close()
        self._finish()
        return self.status

    # --- phases ----------------------------------------------------------------------------

    async def _wait_for_pose(self) -> bool:
        """Zero frames until a fresh, fixed pose arrives; then the room check."""
        config = self._config
        began = self._clock()
        while True:
            if self._stopped():
                return False
            now = self._clock()
            pose = pose_of(self._pose_source(), self._map)
            if (
                pose is not None
                and pose.rtk_fix is RtkFixStatus.FIXED
                and (now - pose.observed_at).total_seconds() <= config.max_pose_age_s
            ):
                break
            if (now - began).total_seconds() > config.start_wait_s:
                fix = pose.rtk_fix.value if pose is not None else "no position"
                self._end("aborted", REASON_RTK_LOST, f"no fresh fixed RTK position ({fix})")
                return False
            if not await self._send(0.0, 0.0):
                return False
            await self._sleep(0.5)
        problem = room_ahead_problem(
            self._pose_source(),
            self._map,
            room_m=config.room_ahead_m,
            margin_m=config.geofence_margin_m,
        )
        if problem is not None:
            self._end("aborted", REASON_GEOFENCE, problem)
            return False
        self._last = _report_of(pose)
        self._set_state("measuring", None)
        return True

    async def _measure_all(self) -> None:
        config = self._config
        assert self._last is not None  # noqa: S101 - set by _wait_for_pose
        origin = (self._last.x, self._last.y)
        covered = 0.0
        last_speed: tuple[float, float] | None = None  # (command, measured speed)
        budget_spent = False
        for command in config.forward_commands:
            duration = config.step_s
            if not budget_spent and last_speed is not None and last_speed[1] > 0:
                predicted = last_speed[1] * command / last_speed[0]
                remaining = config.max_forward_m - covered
                duration = min(duration, remaining / predicted if predicted > 0 else duration)
                if duration < config.min_step_s:
                    budget_spent = True
            if budget_spent:
                self._skip("forward", command)
                continue
            step = await self._step("forward", command, duration, origin=origin)
            if step is None:
                return
            covered = distance(origin, (self._last.x, self._last.y))
            if step.measured is not None and step.measured > config.min_speed_mps:
                last_speed = (command, step.measured)
            if covered >= config.max_forward_m:
                budget_spent = True
        for command in config.turn_commands:
            if await self._step("turn", command, config.step_s, origin=None) is None:
                return
        self._fit(covered)

    def _skip(self, kind: str, command: float) -> None:
        self._steps.append(
            DriveCalibrationStep(
                kind="forward" if kind == "forward" else "turn",
                command=command,
                duration_s=0.0,
                skipped=True,
            )
        )
        self._step_index += 1
        self._notify()

    async def _step(
        self,
        kind: str,
        command: float,
        duration_s: float,
        *,
        origin: tuple[float, float] | None,
    ) -> DriveCalibrationStep | None:
        """Drive one step and settle; the measured step, or None when the run ended."""
        config = self._config
        assert self._last is not None  # noqa: S101 - set by _wait_for_pose
        start = self._clock()
        standing = self._last
        reports: list[_Report] = []
        linear, angular = (command, 0.0) if kind == "forward" else (0.0, command)
        while (self._clock() - start).total_seconds() < duration_s - 1e-9:
            if not await self._send(linear, angular):
                return None
            await self._sleep(1 / config.frame_hz)
            report = self._new_report()
            if report is None:
                if self._ended:
                    return None
                continue
            reports.append(report)
            if (
                origin is not None
                and distance(origin, (report.x, report.y)) >= config.max_forward_m
            ):
                break  # the forward budget is used up
        end = self._clock()
        settle_start = self._clock()
        while (self._clock() - settle_start).total_seconds() < config.settle_s - 1e-9:
            if not await self._send(0.0, 0.0):
                return None
            await self._sleep(1 / config.frame_hz)
            if self._new_report() is None and self._ended:
                return None
        in_step = [r for r in reports if start < r.at <= end]
        step = _measure(
            kind, command, (end - start).total_seconds(), standing, start, in_step, config
        )
        self._steps.append(step)
        self._step_index += 1
        self._notify()
        return step

    def _new_report(self) -> _Report | None:
        """The latest report when it is new (checked against the geofence and the RTK fix);
        None otherwise or when the run ended."""
        pose = pose_of(self._pose_source(), self._map)
        if pose is None or (self._last is not None and pose.observed_at == self._last.at):
            return None
        if pose.rtk_fix is not RtkFixStatus.FIXED:
            self._end("aborted", REASON_RTK_LOST, f"the RTK fix is {pose.rtk_fix.value}")
            return None
        problem = self._fence.violation(pose.point)
        if problem is not None:
            self._end("aborted", REASON_GEOFENCE, f"reported position {problem}")
            return None
        report = _report_of(pose)
        self._last = report
        return report

    def _fit(self, covered: float) -> None:
        config = self._config
        forward = [
            (s.command, s.measured)
            for s in self._steps
            if s.kind == "forward" and s.measured is not None and not s.skipped
        ]
        turns = [(s.command, s.measured) for s in self._steps if s.kind == "turn" and s.measured]
        inverted = [(c, r) for c, r in turns if abs(r) >= config.min_yaw_dps and (c > 0) != (r > 0)]
        if inverted and len(inverted) * 2 > len(turns):
            self._end(
                "failed",
                REASON_TURN_DIRECTION,
                "the mower turned against the command (left must be positive)",
            )
            return
        speed_fit = _fit_line([(c, v) for c, v in forward], config.min_speed_mps)
        if speed_fit is None:
            self._end("failed", REASON_NO_FIT, "the mower did not move forward measurably")
            return
        by_magnitude: dict[float, list[float]] = {}
        for c, r in turns:
            if (c > 0) == (r > 0):
                by_magnitude.setdefault(round(abs(c), 3), []).append(abs(r))
        yaw_fit = _fit_line(
            [(c, sum(rs) / len(rs)) for c, rs in sorted(by_magnitude.items())],
            config.min_yaw_dps,
        )
        if yaw_fit is None:
            self._end("failed", REASON_NO_FIT, "the mower did not turn measurably")
            return
        full_speed, dead_zone = speed_fit
        full_yaw, turn_dead_zone = yaw_fit
        setting = self._setting()
        calibration = DriveCalibration(
            full_speed_mps=round(min(5.0, full_speed), 4),
            full_yaw_rate_dps=round(min(720.0, full_yaw), 3),
            dead_zone=round(dead_zone, 3),
            turn_dead_zone=round(turn_dead_zone, 3),
            drive_speed_setting=setting if setting is not None and 0 < setting <= 5 else None,
            calibrated_at=self._clock(),
        )
        self._result = DriveCalibrationResult(
            calibration=calibration, steps=list(self._steps), forward_m=round(covered, 3)
        )
        self._end("done", None, None)

    # --- frames and state ------------------------------------------------------------------

    def _stopped(self) -> bool:
        if self._ended:
            return True
        if self._stop_reason is not None:
            self._end("aborted", self._stop_reason, self._stop_detail)
            return True
        return False

    async def _send(self, linear: float, angular: float) -> bool:
        if self._stopped():
            return False
        refusal = await self._frames.send(
            linear=linear, angular=angular, blade_on=False, ttl_ms=self._config.ttl_ms
        )
        self._frames_sent += 1
        if refusal is None:
            return True
        self._end("aborted", refusal_reason(refusal), refusal)
        return False

    def _set_state(self, state: DriveCalibrationState, reason: str | None) -> None:
        if (state, reason) == (self._state, self._reason):
            return
        self._state = state
        self._reason = reason
        self._notify()

    def _end(self, state: DriveCalibrationState, reason: str | None, detail: str | None) -> None:
        if self._ended:
            return
        self._ended = True
        self._state = state
        self._reason = reason
        self._detail = detail
        _log.info(
            "calibration %s ends %s%s%s",
            self.run_id,
            state,
            f" ({reason})" if reason else "",
            f": {detail}" if detail else "",
        )

    def _finish(self) -> None:
        if self._finished.is_set():
            return
        if not self._ended:
            self._end("failed", "internal_error", "the calibration ended without a result")
        _log.info("calibration %s: %s", self.run_id, self.summary())
        self._finished.set()
        self._notify()

    def _notify(self) -> None:
        if self._on_status is None:
            return
        try:
            self._on_status(self.status)
        except Exception:
            _log.exception("calibration %s: status callback failed", self.run_id)

    async def _wait(self, seconds: float) -> None:
        with contextlib.suppress(TimeoutError):
            async with asyncio.timeout(seconds):
                await self._wake.wait()


def _report_of(pose: Pose) -> _Report:
    return _Report(pose.observed_at, pose.x, pose.y, pose.theta)


def _measure(
    kind: str,
    command: float,
    duration_s: float,
    standing: _Report,
    start: datetime,
    reports: list[_Report],
    config: CalibrationConfig,
) -> DriveCalibrationStep:
    """One step's measurement (see the module docs)."""
    intervals = [(b.at - a.at).total_seconds() for a, b in itertools.pairwise(reports)]
    interval = sum(intervals) / len(intervals) if intervals else None
    measured: float | None = None
    moved = 0.0
    turned = 0.0
    if reports:
        chain = [standing, *reports]
        moved = sum(distance((a.x, a.y), (b.x, b.y)) for a, b in itertools.pairwise(chain))
        turned = sum(wrap_angle(b.theta - a.theta) for a, b in itertools.pairwise(chain))
        if len(reports) >= 2:
            first, last = reports[0], reports[-1]
            dt = (last.at - first.at).total_seconds()
            pair = [first, *reports[1:]]
        else:
            first, last = standing, reports[0]
            dt = (last.at - start).total_seconds()
            pair = [standing, reports[0]]
        if dt > 0.2:
            if kind == "forward":
                measured = distance((first.x, first.y), (last.x, last.y)) / dt
            else:
                yaw = sum(wrap_angle(b.theta - a.theta) for a, b in itertools.pairwise(pair))
                measured = math.degrees(yaw) / dt
    return DriveCalibrationStep(
        kind="forward" if kind == "forward" else "turn",
        command=command,
        duration_s=round(duration_s, 2),
        measured=round(measured, 4) if measured is not None else None,
        reports=len(reports),
        report_interval_s=round(interval, 3) if interval is not None else None,
        distance_m=round(moved, 3),
        turned_deg=round(math.degrees(turned), 2),
    )


def _fit_line(points: list[tuple[float, float]], minimum: float) -> tuple[float, float] | None:
    """``(full, dead_zone)`` of ``value = full * (c - dz) / (1 - dz)`` through ``points``
    (``(command, value)``); None without a moving point."""
    moving = [(abs(c), v) for c, v in points if v >= minimum and abs(c) > 0]
    if not moving:
        return None
    if len(moving) >= 2:
        n = len(moving)
        mean_c = sum(c for c, _ in moving) / n
        mean_v = sum(v for _, v in moving) / n
        var = sum((c - mean_c) ** 2 for c, _ in moving)
        if var > 1e-9:
            slope = sum((c - mean_c) * (v - mean_v) for c, v in moving) / var
            intercept = mean_v - slope * mean_c
            if slope > 0:
                full = slope + intercept
                dead_zone = max(0.0, min(0.9, -intercept / slope))
                if full > 0:
                    return full, dead_zone
    c, v = max(moving)
    return v / c, 0.0


__all__ = [
    "REASON_NO_FIT",
    "REASON_TURN_DIRECTION",
    "CalibrationConfig",
    "DriveCalibrationRun",
    "calibration_start_problem",
    "room_ahead_problem",
]
