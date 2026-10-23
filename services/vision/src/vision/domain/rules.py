"""Rules of AI assist (pure): which answers may be proposed, when execution aborts, whether
the mower recovered.

Safety rules (ADR 0033, plan "Risiken"):

* An answer with any risk flag, a confidence below the threshold or no step is never proposed;
  an answer ``return_to_dock`` only when :func:`~vision.domain.dock.dock_route` finds it
  applicable (mower in a mapped area linked to the dock, no lift, tilt or other safety stop).
* An answer ``resume`` only for task ``recover`` and when the job context
  (:func:`~vision.domain.job.job_context`) finds a paused job that ``resume_mow`` can continue
  (no spot run, no lift, tilt or other safety stop). Its optional drive steps follow the drive
  rules below; ``resume_mow`` is sent only when :func:`resume_gate` passes (the stuck error
  itself may persist: a LUBA clears it only after the resumed job moved it).
* Dock fallback: when the model recommends nothing (or something not allowed) without any risk
  flag and return to dock is applicable, the service still offers ``return_to_dock``, labelled
  as a rule based fallback. Like every proposal it runs only after a human approves it.
* While executing, the run aborts at once on an emergency stop, on a sensor stop (any error
  that is not ``drive_recoverable``, or the lift flag), on a new error, when the mower starts
  doing something else (mowing, returning, docked), when it goes offline, when no state arrived
  for ``state_timeout`` seconds, when it leaves the geofence (the mapped areas plus a margin, or
  the dock surroundings) or enters a no-go zone, and when a user cancels.
* The Device-Gateway's controller stays the safety authority: these rules only stop sending
  frames early; every frame still carries the dead-man ``ttl_ms`` and ``blade_on = False``.
"""

import math
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Final

from mowdeck_contracts.v1 import (
    LocalPoint,
    MowerActivity,
    MowerErrorCategory,
    MowerMap,
    MowerState,
    VisionTask,
)
from mowdeck_control.geo import distance, point_in_polygon

from vision.domain.context import Pose
from vision.domain.dock import DockRoute, hardware_fault
from vision.domain.job import JobContext
from vision.domain.models import AssessmentOut, ProposalAction, ProposalState, ProposedStep

FRAME_TTL_MS: Final = 1000
"""Dead-man time of every drive frame."""

_BUSY_ACTIVITIES: Final = frozenset(
    {
        MowerActivity.MOWING,
        MowerActivity.RETURNING,
        MowerActivity.DOCKED,
        MowerActivity.CHARGING,
    }
)


def evaluate(
    answer: AssessmentOut,
    *,
    min_confidence: float,
    dock: DockRoute | None = None,
    job: JobContext | None = None,
    task: VisionTask = VisionTask.RECOVER,
) -> tuple[ProposalState, str | None]:
    """``proposed`` or ``rejected_by_ai`` with the reason (the model's answer, checked against
    the dock route and the job)."""
    if answer.risk:
        flags = ", ".join(sorted(r.value for r in answer.risk))
        return ProposalState.REJECTED_BY_AI, f"risk: {flags}"
    if answer.confidence < min_confidence:
        return (
            ProposalState.REJECTED_BY_AI,
            f"confidence {answer.confidence:.2f} below {min_confidence:.2f}",
        )
    if answer.action is ProposalAction.RETURN_TO_DOCK:
        if dock is None or not dock.return_to_dock_applicable:
            why = dock.reason if dock is not None else "no map with a dock is known"
            return ProposalState.REJECTED_BY_AI, f"return to dock is not applicable: {why}"[:500]
        return ProposalState.PROPOSED, None
    if answer.action is ProposalAction.RESUME:
        if task is not VisionTask.RECOVER:
            return ProposalState.REJECTED_BY_AI, f"resume continues the job, the task is {task}"
        if job is None or not job.resumable:
            why = job.resume_note if job is not None else "no mowing job of the mower is known"
            return ProposalState.REJECTED_BY_AI, f"resume is not applicable: {why}"[:500]
        return ProposalState.PROPOSED, None
    if answer.action is ProposalAction.NONE or not answer.proposal:
        reasons = "; ".join(answer.abort_reasons) or "the model proposed no safe maneuver"
        return ProposalState.REJECTED_BY_AI, reasons[:500]
    return ProposalState.PROPOSED, None


@dataclass(frozen=True, slots=True)
class Decision:
    """What becomes of an analysed proposal."""

    state: ProposalState
    detail: str | None
    action: ProposalAction
    fallback: bool = False


FALLBACK_PREFIX: Final = "Fallback: return to dock"


def dock_fallback_applies(answer: AssessmentOut, dock: DockRoute | None) -> bool:
    """Return to dock may be offered although the model did not recommend it: no risk flag and
    the route check finds it applicable."""
    return dock is not None and dock.return_to_dock_applicable and not answer.risk


def decide(
    answer: AssessmentOut,
    *,
    min_confidence: float,
    dock: DockRoute | None,
    allow_fallback: bool = True,
    job: JobContext | None = None,
    task: VisionTask = VisionTask.RECOVER,
) -> Decision:
    """The model's answer through :func:`evaluate`, then the dock fallback (not offered again
    when ``allow_fallback`` is false: an earlier round already tried it without success)."""
    state, detail = evaluate(answer, min_confidence=min_confidence, dock=dock, job=job, task=task)
    if state is ProposalState.PROPOSED:
        action = (
            answer.action
            if answer.action in (ProposalAction.RETURN_TO_DOCK, ProposalAction.RESUME)
            else ProposalAction.DRIVE
        )
        return Decision(state, None, action)
    if allow_fallback and dock_fallback_applies(answer, dock):
        assert dock is not None  # noqa: S101 - checked by dock_fallback_applies
        text = f"{FALLBACK_PREFIX} ({dock.reason}). The model recommended nothing: {detail}"
        return Decision(ProposalState.PROPOSED, text[:500], ProposalAction.RETURN_TO_DOCK, True)
    return Decision(state, detail, answer.action)


def frames_for(step: ProposedStep, rate_hz: float) -> int:
    """Number of frames of a step at ``rate_hz`` (at least one)."""
    return max(1, round(step.seconds * rate_hz))


def total_seconds(steps: Sequence[ProposedStep]) -> float:
    """Planned driving time."""
    return sum(s.seconds for s in steps)


def start_problem(task: VisionTask, state: MowerState | None) -> str | None:
    """Why execution must not start with this state (None: it may)."""
    if state is None:
        return "no state of the mower is known"
    if state.raw.get("lifted") is True:
        return "sensor stop: the mower was lifted"
    if state.activity is MowerActivity.ERROR and not state.drive_recoverable:
        return f"sensor stop: {state.error_code or 'error'} cannot be driven free"
    if state.activity in _BUSY_ACTIVITIES or state.activity is MowerActivity.OFFLINE:
        return f"the mower is {state.activity}"
    if task is VisionTask.FIND_DOCK and state.activity is MowerActivity.ERROR:
        return "the mower reports an error: use recover first"
    return None


_STOP_CATEGORIES: Final = frozenset(
    {MowerErrorCategory.LIFT_TILT, MowerErrorCategory.COLLISION, MowerErrorCategory.BLADE_MOTOR}
)
"""Error categories that are a safety or hardware stop, never something to drive or resume on."""


def resume_gate(state: MowerState | None, initial: MowerState | None = None) -> str | None:
    """Why ``resume_mow`` must not be sent with this state (None: send it).

    The error that paused the job may still be reported: a LUBA clears a stuck error only after
    the resumed job moved it a few metres. Refused are: no state, a lift, offline, an error that
    is not drive recoverable or names a lift, tilt, collision, blade or motor fault, and an
    error other than the one the mower had when the round started (``initial``).
    """
    if state is None:
        return "no state of the mower is known"
    if state.raw.get("lifted") is True:
        return "sensor stop: the mower was lifted"
    if state.activity is MowerActivity.OFFLINE:
        return "the mower is offline"
    if state.activity is not MowerActivity.ERROR:
        return None
    code = state.error_code or "unknown"
    if not state.drive_recoverable:
        return f"sensor stop: {code}"
    if state.error_category in _STOP_CATEGORIES:
        return f"sensor stop: {code} ({state.error_category})"
    fault = hardware_fault(state)
    if fault is not None:
        return fault
    before = initial.error_code if initial and initial.activity is MowerActivity.ERROR else None
    if before != state.error_code:
        return f"new error {code}"
    return None


def persisting_error(state: MowerState, initial: MowerState | None) -> bool:
    """The state still reports the drive recoverable error the round started with (a LUBA keeps
    a stuck error after a manual drive until its job resumed and moved)."""
    return (
        state.activity is MowerActivity.ERROR
        and initial is not None
        and initial.activity is MowerActivity.ERROR
        and resume_gate(state, initial) is None
    )


def state_abort_reason(state: MowerState, initial: MowerState) -> str | None:
    """Why the run must stop now, judged by a new state (None: carry on)."""
    if state.raw.get("lifted") is True:
        return "sensor_stop: lifted"
    if state.activity is MowerActivity.ERROR:
        if not state.drive_recoverable:
            return f"sensor_stop: {state.error_code or 'error'}"
        if initial.activity is not MowerActivity.ERROR:
            return f"new_error: {state.error_code or 'error'}"
        return None
    if state.activity is MowerActivity.OFFLINE:
        return "offline"
    if state.activity in _BUSY_ACTIVITIES:
        return f"activity_changed: {state.activity}"
    return None


def _segment_distance(p: LocalPoint, a: LocalPoint, b: LocalPoint) -> float:
    ax, ay = a
    bx, by = b
    dx, dy = bx - ax, by - ay
    length = dx * dx + dy * dy
    if length == 0:
        return distance(p, a)
    t = max(0.0, min(1.0, ((p[0] - ax) * dx + (p[1] - ay) * dy) / length))
    return distance(p, (ax + t * dx, ay + t * dy))


def polygon_distance(p: LocalPoint, polygon: Sequence[LocalPoint]) -> float:
    """0 inside the polygon, else the distance to its boundary."""
    if point_in_polygon(p, polygon):
        return 0.0
    n = len(polygon)
    return min(_segment_distance(p, polygon[i], polygon[(i + 1) % n]) for i in range(n))


@dataclass(frozen=True, slots=True)
class Geofence:
    """The region automated driving may use: the mapped areas plus ``margin_m``, and the dock
    surroundings (``dock_radius_m``); never a no-go zone or an obstacle."""

    mower_map: MowerMap
    margin_m: float
    dock_radius_m: float = 3.0

    def violation(self, point: LocalPoint) -> str | None:
        """None inside, else what is wrong."""
        for zone in self.mower_map.forbidden_zones():
            if point_in_polygon(point, zone.boundary):
                return f"geofence: inside {zone.id}"
        dock = self.mower_map.dock
        if dock is not None and distance(point, (dock.x, dock.y)) <= self.dock_radius_m:
            return None
        for area in self.mower_map.areas:
            if polygon_distance(point, area.boundary) <= self.margin_m:
                return None
        return "geofence: outside the mapped areas"


def verify(
    task: VisionTask,
    *,
    start: Pose | None,
    end: Pose | None,
    end_state: MowerState | None,
    steps: Sequence[ProposedStep],
    min_move_m: float,
    min_turn_deg: float = 10.0,
    travelled_m: float = 0.0,
    initial: MowerState | None = None,
) -> tuple[bool, str]:
    """Did the maneuver help? The mower must have moved as planned: ``min_move_m`` of path (the
    travelled path counts, so "back, turn, forward" that ends near the start is a move) when the
    plan drove, or a turn when it only turned. It must report no error, except the drive
    recoverable error it had at the start (``initial``): a manual drive does not clear a LUBA's
    stuck error, that happens only once the resumed job moved it."""
    if end_state is None:
        return False, "no state after the maneuver"
    kept = persisting_error(end_state, initial)
    if end_state.activity is MowerActivity.OFFLINE or (
        end_state.activity is MowerActivity.ERROR and not kept
    ):
        return False, f"the mower still reports {end_state.error_code or end_state.activity}"
    if start is None or end is None:
        return False, "the position is unknown, the outcome cannot be checked"
    moved = max(distance((start.x, start.y), (end.x, end.y)), travelled_m)
    drove = any(abs(s.linear) > 0.05 for s in steps)
    if drove and moved < min_move_m:
        return False, f"the mower moved only {moved:.2f} m"
    if not drove:
        if start.theta_rad is None or end.theta_rad is None:
            return False, "the heading is unknown, the turn cannot be checked"
        turned = abs(math.degrees(_angle_diff(end.theta_rad, start.theta_rad)))
        if turned < min_turn_deg:
            return False, f"the mower turned only {turned:.0f} degrees"
    still = (
        f"; the mower still reports {end_state.error_code}, which clears once its job continues"
        if kept
        else ""
    )
    if task is VisionTask.FIND_DOCK:
        return True, f"moved {moved:.2f} m{still}; returning to the dock"
    return True, f"free again, moved {moved:.2f} m{still}"


def _angle_diff(a: float, b: float) -> float:
    return (a - b + math.pi) % (2 * math.pi) - math.pi


def day_start(now: datetime) -> datetime:
    """Start of the UTC day of ``now`` (daily caps)."""
    current = now.astimezone(UTC)
    return current.replace(hour=0, minute=0, second=0, microsecond=0)
