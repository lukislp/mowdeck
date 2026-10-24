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
* Low confidence (ADR 0033 addendum 2026-10-24): an answer below the confidence threshold that
  recommends ``drive``, ``resume`` or ``return_to_dock`` is not discarded. When it passes the
  same checks as any proposal (applicable, steps within the limits, a known position and
  heading, the planned path inside the geofence and outside every no-go zone) it is offered
  marked ``low_confidence``: next to the dock fallback when that applies (the fallback is then
  the default option, the model's recommendation the alternative), or alone. When it fails a
  check it is not offered and the reason is kept (``model_refused``). A human picks the
  option; nothing runs without that approval. Above the threshold nothing changes.
* While executing, the run aborts at once on an emergency stop, on a sensor stop (any error
  that is not ``drive_recoverable``, or the lift flag), on a new error, when the mower starts
  doing something else (mowing, returning, docked), when it goes offline, when no state arrived
  for ``state_timeout`` seconds, when it leaves the geofence (the mapped areas plus a margin, or
  the dock surroundings) or enters a no-go zone, and when a user cancels.
* The Device-Gateway's controller stays the safety authority: these rules only stop sending
  frames early; every frame still carries the dead-man ``ttl_ms`` and ``blade_on = False``.
"""

import math
from collections.abc import Callable, Sequence
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

from vision.domain.context import Motion, Pose
from vision.domain.dock import DockRoute, hardware_fault
from vision.domain.job import JobContext
from vision.domain.models import (
    MAX_STEP_SECONDS,
    MAX_STEPS,
    AssessmentOut,
    ProposalAction,
    ProposalState,
    ProposedStep,
)

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
    """What becomes of an analysed proposal.

    ``action`` is the default option: what an approval without a choice executes.
    ``alternative`` is the model's low-confidence recommendation offered next to a default dock
    fallback.
    """

    state: ProposalState
    detail: str | None
    action: ProposalAction
    fallback: bool = False
    low_confidence: bool = False
    """The model's recommendation is offered although its confidence is below the threshold
    (as ``action`` when it is offered alone, as ``alternative`` next to the fallback)."""
    alternative: ProposalAction | None = None
    """The model's low-confidence recommendation next to the default dock fallback."""
    model_refused: str | None = None
    """Why the model's low-confidence recommendation is not offered (a failed check)."""


FALLBACK_PREFIX: Final = "Fallback: return to dock"

PathCheck = Callable[[Sequence[ProposedStep]], str | None]
"""Checks planned drive steps against the mower's position and the geofence (None: fine)."""

_OFFERABLE: Final = frozenset(
    {ProposalAction.DRIVE, ProposalAction.RESUME, ProposalAction.RETURN_TO_DOCK}
)


def dock_fallback_applies(answer: AssessmentOut, dock: DockRoute | None) -> bool:
    """Return to dock may be offered although the model did not recommend it: no risk flag and
    the route check finds it applicable."""
    return dock is not None and dock.return_to_dock_applicable and not answer.risk


def recommends(answer: AssessmentOut) -> bool:
    """The model recommended a maneuver (``drive`` with steps, ``resume`` or
    ``return_to_dock``), whatever its confidence."""
    if answer.action is ProposalAction.DRIVE:
        return bool(answer.proposal)
    return answer.action in _OFFERABLE


def _executed_action(answer: AssessmentOut) -> ProposalAction:
    return (
        answer.action
        if answer.action in (ProposalAction.RETURN_TO_DOCK, ProposalAction.RESUME)
        else ProposalAction.DRIVE
    )


def low_confidence_problem(
    answer: AssessmentOut,
    *,
    dock: DockRoute | None,
    job: JobContext | None,
    task: VisionTask,
    path_check: PathCheck | None,
) -> str | None:
    """Why the model's low-confidence recommendation may not be offered (None: it may): the
    same checks as any proposal (:func:`evaluate` without the threshold), and its drive steps
    must pass ``path_check``. Without a check the steps are refused: a path that cannot be
    checked is never offered below the threshold."""
    state, why = evaluate(answer, min_confidence=0.0, dock=dock, job=job, task=task)
    if state is not ProposalState.PROPOSED:
        return why or "the recommendation is not applicable"
    if answer.action is ProposalAction.RETURN_TO_DOCK or not answer.proposal:
        return None
    if path_check is None:
        return "the planned path cannot be checked"
    return path_check(answer.proposal)


def decide(
    answer: AssessmentOut,
    *,
    min_confidence: float,
    dock: DockRoute | None,
    allow_fallback: bool = True,
    job: JobContext | None = None,
    task: VisionTask = VisionTask.RECOVER,
    path_check: PathCheck | None = None,
) -> Decision:
    """The model's answer through :func:`evaluate`, then the dock fallback (not offered again
    when ``allow_fallback`` is false: an earlier round already tried it without success).

    Below the threshold a recommendation that passes :func:`low_confidence_problem` is offered
    marked ``low_confidence``: next to the fallback (which stays the default option) or alone.
    A risk flag still refuses everything; above the threshold nothing changes."""
    if not answer.risk and answer.confidence < min_confidence and recommends(answer):
        return _decide_low_confidence(
            answer,
            min_confidence=min_confidence,
            dock=dock,
            allow_fallback=allow_fallback,
            job=job,
            task=task,
            path_check=path_check,
        )
    state, detail = evaluate(answer, min_confidence=min_confidence, dock=dock, job=job, task=task)
    if state is ProposalState.PROPOSED:
        return Decision(state, None, _executed_action(answer))
    if allow_fallback and dock_fallback_applies(answer, dock):
        assert dock is not None  # noqa: S101 - checked by dock_fallback_applies
        lead = (
            f"The model's {answer.action.value} is not offered"
            if recommends(answer)
            else "The model recommended nothing"
        )
        text = f"{FALLBACK_PREFIX} ({dock.reason}). {lead}: {detail}"
        return Decision(ProposalState.PROPOSED, text[:500], ProposalAction.RETURN_TO_DOCK, True)
    return Decision(state, detail, answer.action)


def _decide_low_confidence(
    answer: AssessmentOut,
    *,
    min_confidence: float,
    dock: DockRoute | None,
    allow_fallback: bool,
    job: JobContext | None,
    task: VisionTask,
    path_check: PathCheck | None,
) -> Decision:
    below = f"confidence {answer.confidence:.2f} below {min_confidence:.2f}"
    action = _executed_action(answer)
    problem = low_confidence_problem(answer, dock=dock, job=job, task=task, path_check=path_check)
    fallback = allow_fallback and dock_fallback_applies(answer, dock)
    if problem is None and (action is ProposalAction.RETURN_TO_DOCK or not fallback):
        text = f"The model recommends {action.value} with low confidence ({below})."
        return Decision(ProposalState.PROPOSED, text, action, low_confidence=True)
    if problem is None:
        assert dock is not None  # noqa: S101 - fallback implies a dock route
        text = (
            f"{FALLBACK_PREFIX} ({dock.reason}). The model recommends {action.value} with low "
            f"confidence ({below}): offered as the alternative."
        )
        return Decision(
            ProposalState.PROPOSED,
            text[:500],
            ProposalAction.RETURN_TO_DOCK,
            fallback=True,
            low_confidence=True,
            alternative=action,
        )
    refused = problem[:300]
    if fallback:
        assert dock is not None  # noqa: S101 - fallback implies a dock route
        text = (
            f"{FALLBACK_PREFIX} ({dock.reason}). The model's {action.value} ({below}) is not "
            f"offered: {refused}"
        )
        return Decision(
            ProposalState.PROPOSED,
            text[:500],
            ProposalAction.RETURN_TO_DOCK,
            fallback=True,
            model_refused=refused,
        )
    text = f"{below}; the model's {action.value} is not offered: {refused}"
    return Decision(ProposalState.REJECTED_BY_AI, text[:500], answer.action, model_refused=refused)


def path_problem(
    steps: Sequence[ProposedStep],
    *,
    pose: Pose | None,
    fence: "Geofence | None",
    motion: Motion,
    sample_seconds: float = 0.1,
) -> str | None:
    """Why planned drive steps must not be offered (None: they may): the step limits, a known
    position (and heading when the plan drives), a start inside the geofence, and the
    dead-reckoned path (``motion``'s typical speed and turn rate) never leaving the geofence or
    entering a no-go zone. Blades stay off whatever the steps say: no step can carry them."""
    if not steps:
        return None
    if len(steps) > MAX_STEPS:
        return f"more than {MAX_STEPS} steps"
    for index, step in enumerate(steps, 1):
        if not 0 < step.seconds <= MAX_STEP_SECONDS:
            return f"step {index} lasts {step.seconds:g} s (at most {MAX_STEP_SECONDS:g} s)"
        if abs(step.linear) > 1 or abs(step.angular) > 1:
            return f"step {index} is outside the drive limits"
    if fence is None:
        return "no map of the mower: the geofence cannot be checked"
    if pose is None:
        return "the position of the mower is unknown"
    if pose.theta_rad is None and any(abs(step.linear) > 0 for step in steps):
        return "the heading of the mower is unknown: the planned path cannot be checked"
    start = fence.violation((pose.x, pose.y))
    if start is not None:
        return start
    x, y, theta = pose.x, pose.y, pose.theta_rad or 0.0
    turn = math.radians(motion.turn_deg_per_s_at_1)
    for index, step in enumerate(steps, 1):
        ticks = max(1, math.ceil(step.seconds / sample_seconds))
        dt = step.seconds / ticks
        for _ in range(ticks):
            theta += step.angular * turn * dt
            speed = step.linear * motion.linear_mps_at_1
            x += speed * math.cos(theta) * dt
            y += speed * math.sin(theta) * dt
            violation = fence.violation((x, y))
            if violation is not None:
                return f"step {index} leaves the safe area ({violation})"
    return None


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
