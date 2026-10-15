"""Drive calibration (ADR 0042): :class:`DriveCalibrationRun` measures what the stick values
mean on the ground.

A spot run (ADR 0039) steers with normalised ``linear`` and ``angular`` values; how fast a mower
drives and turns at a given value depends on the model, the firmware and vendor settings. The
calibration drives a few steps over the generic drive channel and reads only the neutral state
(position, heading, RTK fix, ``observed_at``):

* forward at ``linear`` 0.3, 0.5, 0.7 and 1.0 (low to high), each up to ``step_s`` (default
  6 s);
* turns in place at ``angular`` +0.3, -0.3, +0.6, -0.6, +1.0 and -1.0, each ``step_s``;
* zero frames for ``pause_s`` (default 2 s) after every step, so the mower stops before the next
  one.

Mowers report their pose only about once a second and react one to two seconds late, so a step
is measured over its *steady window* only: the reports from ``settle_s`` (1.5 s) after the
step's first frame to its last frame. The ground speed is the distance between the first and the
last report of the window divided by the time between them, the yaw rate the unwrapped heading
change over the same reports divided by that time. A window shorter than ``min_window_s`` (3 s)
or with fewer than ``min_reports`` (4) reports makes the step ``invalid``: it has no value and
never counts as standing still.

Before the fit the measurements are checked: a turn and its opposite turn of the same magnitude
must have opposite signs and agree in magnitude within ``turn_pair_tolerance`` (50 %), else
both are ``inconsistent``; speed and yaw rate must not fall as the stick grows (within
``monotonic_tolerance``), points that break this are ``outlier``. Both are left out of the fit.
It then fits

``speed(c) = full_speed * (c - dead_zone) / (1 - dead_zone)`` for ``c > dead_zone``

by least squares over at least two moving forward steps (the same for the yaw rate over the
turn magnitudes, both directions averaged) and returns a
:class:`~mowdeck_contracts.v1.DriveCalibrationResult`; with fewer the calibration fails
(``no_fit``, the detail says which axis and why).

Safety, the same for every manufacturer:

* blades off in every frame; the frames go through the controller (dead-man, sensor stops);
* before the first step the mower must stand idle out of its dock with a fresh, fixed RTK pose
  (zero frames keep the drive session open while it waits, at most ``start_wait_s``) and the
  straight line of ``room_ahead_m`` along its heading must lie inside the geofence (the mapped
  areas inset by the margin, minus the no-go zones);
* the forward steps stop once ``max_forward_m`` were covered: a step is shortened when the
  speeds measured so far predict it would pass the budget, skipped (with every later forward
  step) when it would become too short for a steady window, and cut when the reported distance
  plus the expected run-on reaches the budget; every report is checked against the geofence;
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
from datetime import datetime, timedelta
from typing import Final
from uuid import UUID

from mowdeck_contracts.v1 import (
    DEFAULT_CALIBRATION_STEP_S,
    CalibrationStepOutcome,
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
    weak_link_problem,
)

_log = logging.getLogger("mowdeck_control.calibration")

REASON_NO_FIT: Final = "no_fit"
"""``failed``: the measurements do not allow a fit (``detail`` says why)."""
REASON_TURN_DIRECTION: Final = "turn_direction"
"""``failed``: the mower turned the other way than commanded (left must be positive)."""

NOTE_TOO_FEW_REPORTS: Final = "too few reports"
NOTE_BUDGET: Final = "the forward distance budget is used up"
NOTE_INCONSISTENT: Final = "the opposite turn disagrees"
NOTE_OUTLIER: Final = "slower than a smaller stick value"


@dataclass(frozen=True, slots=True)
class CalibrationConfig:
    """Tunable behaviour of a :class:`DriveCalibrationRun` (the defaults are the design
    values)."""

    forward_commands: tuple[float, ...] = (0.3, 0.5, 0.7, 1.0)
    """Forward stick values, driven in this order (low to high, so the budget check can stop
    early)."""
    turn_commands: tuple[float, ...] = (0.3, -0.3, 0.6, -0.6, 1.0, -1.0)
    step_s: float = DEFAULT_CALIBRATION_STEP_S
    """Duration of each step (forward steps may be shorter, see ``max_forward_m``)."""
    settle_s: float = 1.5
    """Start of each step left out of the measurement: the mower reacts late and speeds up."""
    pause_s: float = 2.0
    """Zero frames after each step, so the mower stops before the next one."""
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
    min_reports: int = 4
    """Reports a steady window needs; fewer make the step ``invalid``."""
    min_window_s: float = 3.0
    """Shortest steady window; a shorter one makes the step ``invalid``."""
    min_speed_mps: float = 0.005
    """A step slower than this counts as standing still (below the dead zone)."""
    min_yaw_dps: float = 0.2
    """A turn slower than this counts as not turning."""
    turn_pair_tolerance: float = 0.5
    """Largest difference of a turn and its opposite turn, relative to the larger one."""
    monotonic_tolerance: float = 0.15
    """How far a value may fall below a smaller stick's value (relative) before it is an
    outlier."""
    forward_safety: float = 1.25
    """Factor on the predicted speed when a forward step is shortened for the budget."""
    report_interval_s: float = 0.5
    """Interval of fresh reports the controller asks the connection for while the calibration
    runs (as fast as the link allows, never faster than this)."""

    @property
    def min_step_s(self) -> float:
        """Shortest step that can still give a valid measurement."""
        return self.settle_s + self.min_window_s


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
    battery, and no drive link reporting a signal below
    :data:`~mowdeck_contracts.v1.LINK_MIN_RSSI_DBM` (:func:`weak_link_problem`)."""
    if state.activity is not MowerActivity.IDLE:
        return f"the mower is {state.activity.value}; a calibration needs it idle out of its dock"
    age = (now - state.observed_at).total_seconds()
    if age > 60.0:
        return f"the mower's state is {age:.0f} s old; wait for a fresh report"
    if state.rtk_fix is not RtkFixStatus.FIXED:
        return f"no fixed RTK position (the fix is {state.rtk_fix.value})"
    if state.battery_percent <= 20:
        return f"battery {state.battery_percent} % is too low for a calibration"
    return weak_link_problem(state)


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
        moving: list[tuple[float, float]] = []  # (command, measured speed) of valid steps
        budget_spent = False
        commands = config.forward_commands
        for index, command in enumerate(commands):
            duration = config.step_s
            if not budget_spent:
                predicted = _predict_speed(moving, command)
                if predicted is not None:
                    remaining = config.max_forward_m - covered
                    duration = min(duration, remaining / (predicted * config.forward_safety))
                if duration < self._shortest_step_s() - 1e-9:
                    budget_spent = True
            if budget_spent:
                self._skip("forward", command)
                continue
            # an equal share of what is left for each forward step still to come: a step ends
            # early (once its window is valid) when it would use more
            share = (config.max_forward_m - covered) / (len(commands) - index)
            outcome = await self._step(
                "forward", command, duration, origin=origin, share_until_m=covered + share
            )
            if outcome is None:
                return
            step, cut = outcome
            covered = distance(origin, (self._last.x, self._last.y))
            if (
                step.outcome == "measured"
                and step.measured is not None
                and step.measured > config.min_speed_mps
            ):
                moving.append((command, step.measured))
            if cut or covered >= config.max_forward_m:
                budget_spent = True
        for command in config.turn_commands:
            if await self._step("turn", command, config.step_s, origin=None) is None:
                return
        self._fit(covered)

    def _shortest_step_s(self) -> float:
        """The shortest forward step that can still be valid: the settling time plus a window
        of ``min_window_s`` that also holds ``min_reports`` reports at the report interval seen
        so far."""
        config = self._config
        intervals = [s.report_interval_s for s in self._steps if s.report_interval_s]
        if not intervals:
            return config.min_step_s
        interval = sum(intervals) / len(intervals)
        return config.settle_s + max(config.min_window_s, config.min_reports * interval)

    def _skip(self, kind: str, command: float) -> None:
        self._steps.append(
            DriveCalibrationStep(
                kind="forward" if kind == "forward" else "turn",
                command=command,
                duration_s=0.0,
                skipped=True,
                outcome="skipped",
                note=NOTE_BUDGET,
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
        share_until_m: float | None = None,
    ) -> tuple[DriveCalibrationStep, bool] | None:
        """Drive one step and pause; the measured step and whether the forward budget cut it
        short, or None when the run ended. A forward step also ends once its steady window is
        valid and it reaches ``share_until_m`` from the origin (with the expected run-on)."""
        config = self._config
        start = self._clock()
        window_start = start + timedelta(seconds=config.settle_s)
        reports: list[_Report] = []
        cut = False
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
            if origin is None:
                continue
            reach = self._reach(origin, reports)
            if reach >= config.max_forward_m:
                cut = True
                break
            windowed = sum(1 for r in reports if r.at >= window_start)
            valid = (
                windowed >= config.min_reports
                and (self._clock() - window_start).total_seconds() >= config.min_window_s - 1e-9
            )
            if valid and share_until_m is not None and reach >= share_until_m:
                break
        end = self._clock()
        pause_start = self._clock()
        while (self._clock() - pause_start).total_seconds() < config.pause_s - 1e-9:
            if not await self._send(0.0, 0.0):
                return None
            await self._sleep(1 / config.frame_hz)
            if self._new_report() is None and self._ended:
                return None
        in_window = [r for r in reports if window_start <= r.at <= end]
        step = _measure(
            kind,
            command,
            duration_s=(end - start).total_seconds(),
            window_s=max(0.0, (end - window_start).total_seconds()),
            reports=in_window,
            config=config,
        )
        self._steps.append(step)
        self._step_index += 1
        self._notify()
        return step, cut

    def _reach(self, origin: tuple[float, float], reports: list[_Report]) -> float:
        """Distance from ``origin`` the mower will have covered when it stops now: the latest
        report plus the expected run-on (the speed between the last two reports for
        ``settle_s``, the mower reacts that late to the stop)."""
        last = reports[-1]
        covered = distance(origin, (last.x, last.y))
        run_on = 0.0
        if len(reports) >= 2:
            prev = reports[-2]
            dt = (last.at - prev.at).total_seconds()
            if dt > 0:
                speed = distance((prev.x, prev.y), (last.x, last.y)) / dt
                run_on = speed * self._config.settle_s
        return covered + run_on

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

    def _mark(self, index: int, outcome: CalibrationStepOutcome, note: str) -> None:
        self._steps[index] = self._steps[index].model_copy(
            update={"outcome": outcome, "note": note}
        )

    def _usable(self, kind: str) -> list[tuple[int, float, DriveCalibrationStep]]:
        """``(index, value, step)`` of the steps of ``kind`` that still count."""
        return [
            (i, s.measured, s)
            for i, s in enumerate(self._steps)
            if s.kind == kind and s.outcome == "measured" and s.measured is not None
        ]

    def _fit(self, covered: float) -> None:
        config = self._config
        turns = self._usable("turn")
        inverted = [
            s
            for _, value, s in turns
            if abs(value) >= config.min_yaw_dps and (s.command > 0) != (value > 0)
        ]
        if inverted and len(inverted) * 2 > len(turns):
            self._end(
                "failed",
                REASON_TURN_DIRECTION,
                "the mower turned against the command (left must be positive)",
            )
            return
        self._check_turn_pairs()
        forward = self._monotonic(
            [(s.command, value, [i]) for i, value, s in self._usable("forward")],
            config.min_speed_mps,
        )
        speed_fit = _fit_line(forward, config.min_speed_mps)
        if speed_fit is None:
            self._end("failed", REASON_NO_FIT, self._no_fit_detail("forward", forward))
            return
        by_magnitude: dict[float, list[tuple[int, float]]] = {}
        for i, value, s in self._usable("turn"):
            # left positive: a right turn's yaw counts with the opposite sign
            yaw = value if s.command > 0 else -value
            by_magnitude.setdefault(round(abs(s.command), 3), []).append((i, yaw))
        yaw_points = self._monotonic(
            [
                (c, sum(y for _, y in group) / len(group), [i for i, _ in group])
                for c, group in sorted(by_magnitude.items())
            ],
            config.min_yaw_dps,
        )
        yaw_fit = _fit_line(yaw_points, config.min_yaw_dps)
        if yaw_fit is None:
            self._end("failed", REASON_NO_FIT, self._no_fit_detail("turn", yaw_points))
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

    def _check_turn_pairs(self) -> None:
        """Mark a turn and its opposite turn ``inconsistent`` when their signs do not differ or
        their magnitudes differ by more than ``turn_pair_tolerance``. A turn whose opposite has
        no value is kept (nothing contradicts it)."""
        config = self._config
        pairs: dict[float, dict[bool, tuple[int, float]]] = {}
        for i, value, s in self._usable("turn"):
            pairs.setdefault(round(abs(s.command), 3), {})[s.command > 0] = (i, value)
        for pair in pairs.values():
            if len(pair) < 2:
                continue
            (left, a), (right, b) = pair[True], pair[False]
            if abs(a) < config.min_yaw_dps and abs(b) < config.min_yaw_dps:
                continue  # both stand: consistent (below the dead zone)
            larger = max(abs(a), abs(b))
            opposite = a * b < 0
            agree = abs(abs(a) - abs(b)) <= config.turn_pair_tolerance * larger
            if opposite and agree:
                continue
            self._mark(left, "inconsistent", NOTE_INCONSISTENT)
            self._mark(right, "inconsistent", NOTE_INCONSISTENT)

    def _monotonic(
        self, points: list[tuple[float, float, list[int]]], minimum: float
    ) -> list[tuple[float, float]]:
        """The points ``(command, value, step indexes)`` that grow with the stick (the longest
        chain in which no value falls more than ``monotonic_tolerance`` below an earlier one,
        of two equally long ones the faster: a mower rather stalls than speeds up by error);
        the others are marked ``outlier``. Returns ``(command, value)`` of the kept ones."""
        tolerance = self._config.monotonic_tolerance
        ordered = sorted(points, key=lambda p: abs(p[0]))
        n = len(ordered)
        if n == 0:
            return []
        best = [(1, ordered[k][1]) for k in range(n)]  # (chain length, sum of its values)
        prev = [-1] * n
        for j in range(n):
            for i in range(j):
                grows = ordered[j][1] >= ordered[i][1] * (1 - tolerance) - minimum
                candidate = (best[i][0] + 1, best[i][1] + ordered[j][1])
                if grows and candidate > best[j]:
                    best[j], prev[j] = candidate, i
        j = max(range(n), key=lambda k: best[k])
        keep: set[int] = set()
        while j >= 0:
            keep.add(j)
            j = prev[j]
        for k, (_, _, indexes) in enumerate(ordered):
            if k not in keep:
                for index in indexes:
                    self._mark(index, "outlier", NOTE_OUTLIER)
        return [(abs(ordered[k][0]), ordered[k][1]) for k in sorted(keep)]

    def _no_fit_detail(self, kind: str, points: list[tuple[float, float]]) -> str:
        """Why the axis ``kind`` has no fit: the usable steps and what became of the others."""
        config = self._config
        minimum = config.min_speed_mps if kind == "forward" else config.min_yaw_dps
        steps = [s for s in self._steps if s.kind == kind]
        axis = "forward steps" if kind == "forward" else "turns"
        moving = sum(1 for _, v in points if v >= minimum)
        if moving >= 2:
            return f"the {axis} do not grow with the stick"
        counts = {
            outcome: sum(1 for s in steps if s.outcome == outcome)
            for outcome in ("skipped", "invalid", "inconsistent", "outlier")
        }
        dropped = ", ".join(f"{n} {o}" for o, n in counts.items() if n)
        if kind == "forward":
            detail = f"only {moving} of {len(steps)} forward steps usable, 2 needed"
        else:
            magnitudes = len({round(abs(s.command), 3) for s in steps})
            detail = f"only {moving} of {magnitudes} turn magnitudes usable, 2 needed"
        if dropped:
            detail += f" ({dropped})"
        if kind == "forward" and counts["skipped"]:
            detail += (
                f"; too fast for the {config.max_forward_m:g} m forward budget, lower the speed "
                "for manual driving and calibrate again"
            )
        return detail

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
    *,
    duration_s: float,
    window_s: float,
    reports: list[_Report],
    config: CalibrationConfig,
) -> DriveCalibrationStep:
    """One step's measurement over the reports of its steady window (see the module docs)."""
    intervals = [(b.at - a.at).total_seconds() for a, b in itertools.pairwise(reports)]
    interval = sum(intervals) / len(intervals) if intervals else None
    moved = sum(distance((a.x, a.y), (b.x, b.y)) for a, b in itertools.pairwise(reports))
    turned = sum(wrap_angle(b.theta - a.theta) for a, b in itertools.pairwise(reports))
    span = (reports[-1].at - reports[0].at).total_seconds() if reports else 0.0
    step = DriveCalibrationStep(
        kind="forward" if kind == "forward" else "turn",
        command=command,
        duration_s=round(duration_s, 2),
        reports=len(reports),
        report_interval_s=round(interval, 3) if interval is not None else None,
        distance_m=round(moved, 3),
        turned_deg=round(math.degrees(turned), 2),
        window_s=round(window_s, 2),
    )
    if len(reports) < config.min_reports or window_s < config.min_window_s - 1e-6 or span <= 0:
        note = (
            f"{NOTE_TOO_FEW_REPORTS}: {len(reports)} in {window_s:.1f} s "
            f"({config.min_reports} in {config.min_window_s:g} s needed)"
        )
        return step.model_copy(update={"outcome": "invalid", "note": note})
    first, last = reports[0], reports[-1]
    if kind == "forward":
        measured = distance((first.x, first.y), (last.x, last.y)) / span
    else:
        measured = math.degrees(turned) / span
    return step.model_copy(update={"measured": round(measured, 4)})


def _predict_speed(moving: list[tuple[float, float]], command: float) -> float | None:
    """The speed expected at ``command`` from the moving forward steps so far: proportional to
    the last one, or the line through the last two when that is faster (a dead zone makes the
    proportional guess too low); None without a moving step."""
    if not moving:
        return None
    c2, v2 = moving[-1]
    predicted = v2 * command / c2
    if len(moving) >= 2:
        c1, v1 = moving[-2]
        if c2 != c1:
            predicted = max(predicted, v2 + (v2 - v1) / (c2 - c1) * (command - c2))
    return predicted if predicted > 0 else None


def _fit_line(points: list[tuple[float, float]], minimum: float) -> tuple[float, float] | None:
    """``(full, dead_zone)`` of ``value = full * (c - dz) / (1 - dz)`` through ``points``
    (``(command, value)``) by least squares over the moving ones; None with fewer than two
    moving points or a line that does not rise."""
    moving = [(abs(c), v) for c, v in points if v >= minimum and abs(c) > 0]
    if len(moving) < 2:
        return None
    n = len(moving)
    mean_c = sum(c for c, _ in moving) / n
    mean_v = sum(v for _, v in moving) / n
    var = sum((c - mean_c) ** 2 for c, _ in moving)
    if var <= 1e-9:
        return None
    slope = sum((c - mean_c) * (v - mean_v) for c, v in moving) / var
    if slope <= 0:
        return None
    intercept = mean_v - slope * mean_c
    full = slope + intercept
    if full <= 0:
        return None
    return full, max(0.0, min(0.9, -intercept / slope))


__all__ = [
    "NOTE_BUDGET",
    "NOTE_INCONSISTENT",
    "NOTE_OUTLIER",
    "NOTE_TOO_FEW_REPORTS",
    "REASON_NO_FIT",
    "REASON_TURN_DIRECTION",
    "CalibrationConfig",
    "DriveCalibrationRun",
    "calibration_start_problem",
    "room_ahead_problem",
]
