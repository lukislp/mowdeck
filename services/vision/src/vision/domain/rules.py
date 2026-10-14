"""Rules of AI assist (pure): which answers may be proposed, when execution aborts, whether
the mower recovered.

Safety rules (ADR 0033, plan "Risiken"):

* An answer with any risk flag, a confidence below the threshold or no step is never proposed.
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

from mowdeck_contracts.v1 import LocalPoint, MowerActivity, MowerMap, MowerState, VisionTask
from mowdeck_control.geo import distance, point_in_polygon

from vision.domain.context import Pose
from vision.domain.models import AssessmentOut, ProposalState, ProposedStep

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


def evaluate(answer: AssessmentOut, *, min_confidence: float) -> tuple[ProposalState, str | None]:
    """``proposed`` or ``rejected_by_ai`` with the reason."""
    if answer.risk:
        flags = ", ".join(sorted(r.value for r in answer.risk))
        return ProposalState.REJECTED_BY_AI, f"risk: {flags}"
    if answer.confidence < min_confidence:
        return (
            ProposalState.REJECTED_BY_AI,
            f"confidence {answer.confidence:.2f} below {min_confidence:.2f}",
        )
    if not answer.proposal:
        reasons = "; ".join(answer.abort_reasons) or "the model proposed no safe maneuver"
        return ProposalState.REJECTED_BY_AI, reasons[:500]
    return ProposalState.PROPOSED, None


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
) -> tuple[bool, str]:
    """Did the maneuver help? The mower must report no error and must have moved as planned:
    ``min_move_m`` of path (the travelled path counts, so "back, turn, forward" that ends near
    the start is a move) when the plan drove, or a turn when it only turned."""
    if end_state is None:
        return False, "no state after the maneuver"
    if end_state.activity in (MowerActivity.ERROR, MowerActivity.OFFLINE):
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
    if task is VisionTask.FIND_DOCK:
        return True, f"moved {moved:.2f} m; returning to the dock"
    return True, f"free again, moved {moved:.2f} m"


def _angle_diff(a: float, b: float) -> float:
    return (a - b + math.pi) % (2 * math.pi) - math.pi


def day_start(now: datetime) -> datetime:
    """Start of the UTC day of ``now`` (daily caps)."""
    current = now.astimezone(UTC)
    return current.replace(hour=0, minute=0, second=0, microsecond=0)
