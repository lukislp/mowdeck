"""The validation chain: the same checks for every plan, whatever pattern produced it.

1. Every segment stays inside the area minus the safety margin (holes and zones included).
2. No segment with the blades on crosses a no-go zone, an obstacle or a hole.
3. Transfers have the blades off; speeds are positive and within the mower's limits.
4. Turns are feasible for the turning radius: at each heading change of ``theta`` the mower
   needs ``r * tan(theta / 2)`` of straight track before and after the corner to fit an arc of
   radius ``r``; a reversal is impossible for ``r > 0``. Mowers that turn on the spot use
   ``r = 0`` and pass any turn.

:func:`check` returns the findings, :func:`validate` raises :class:`PlanRejected` with them.
A plug-in therefore cannot produce an unsafe route that reaches a mower: the Planner always
validates before it answers.
"""

import math
from collections.abc import Sequence

from shapely import prepare
from shapely.geometry import LineString
from shapely.geometry.base import BaseGeometry

from mowdeck_patterns.geometry import EPS, heading_change_deg, inset
from mowdeck_patterns.models import Area, MowerSpec, MowPlan, PlanRejected, Point

TOLERANCE_M = 1e-3
"""Numerical slack of the containment checks (one millimetre)."""
MAX_REASONS = 12
"""At most this many findings are reported (the rest are summarised)."""
_SPEED_SLACK = 1e-6


def _where(geometry: BaseGeometry) -> str:
    point = geometry.representative_point()
    return f"({point.x:.2f}, {point.y:.2f})"


def check(plan: MowPlan, area: Area, mower: MowerSpec) -> list[str]:
    """Run the validation chain and return every finding (empty: the plan is valid)."""
    reasons: list[str] = []
    if not plan.segments:
        return ["the plan has no segments"]
    region = area.region()
    safe = inset(region, mower.safety_margin_m).buffer(TOLERANCE_M)
    prepare(safe)
    blocked = area.forbidden()
    holes = area.outline()
    for index, segment in enumerate(plan.segments, start=1):
        line = LineString(segment.polyline)
        label = f"segment {index} ({segment.kind})"
        if not safe.covers(line):
            outside = line.difference(safe)
            reasons.append(
                f"{label} leaves the area or its safety margin of {mower.safety_margin_m:.2f} m "
                f"near {_where(outside if not outside.is_empty else line)}"
            )
        if segment.blades_on:
            hit = None
            if not blocked.is_empty and line.intersection(blocked).length > TOLERANCE_M:
                hit = line.intersection(blocked)
                reasons.append(f"{label} cuts through a no-go zone or obstacle near {_where(hit)}")
            outside_outline = line.difference(holes.buffer(TOLERANCE_M))
            if hit is None and outside_outline.length > TOLERANCE_M:
                reasons.append(
                    f"{label} cuts outside the area or through a hole near "
                    f"{_where(outside_outline)}"
                )
        if segment.kind == "transfer" and segment.blades_on:
            reasons.append(f"{label} is a transfer with the blades on")
        limit = (
            mower.max_speed_mps
            if segment.blades_on
            else max(mower.max_speed_mps, mower.transfer_speed_mps)
        )
        if not 0 < segment.speed <= limit + _SPEED_SLACK:
            reasons.append(
                f"{label} has speed {segment.speed:.2f} m/s, the limit is {limit:.2f} m/s"
            )
    reasons.extend(turn_findings(plan, mower))
    if len(reasons) > MAX_REASONS:
        rest = len(reasons) - MAX_REASONS
        reasons = [*reasons[:MAX_REASONS], f"... and {rest} more findings"]
    return reasons


def validate(plan: MowPlan, area: Area, mower: MowerSpec) -> MowPlan:
    """Return ``plan`` unchanged, or raise :class:`PlanRejected` with every finding."""
    reasons = check(plan, area, mower)
    if reasons:
        raise PlanRejected(reasons)
    return plan


def _track(plan: MowPlan) -> list[list[Point]]:
    """Continuous pieces of the plan (consecutive segments that share their end points)."""
    pieces: list[list[Point]] = []
    current: list[Point] = []
    for segment in plan.segments:
        points = list(segment.polyline)
        if current and math.dist(current[-1], points[0]) <= EPS:
            points = points[1:]
        elif current:
            pieces.append(current)
            current = []
        for point in points:
            if not current or math.dist(current[-1], point) > EPS:
                current.append(point)
    if current:
        pieces.append(current)
    return pieces


def turn_findings(plan: MowPlan, mower: MowerSpec) -> list[str]:
    """Corners the turning radius cannot drive (none for ``turning_radius_m == 0``)."""
    radius = mower.turning_radius_m
    if radius <= 0:
        return []
    findings: list[str] = []
    for piece in _track(plan):
        if len(piece) < 3:
            continue
        need = [0.0] * len(piece)
        for i in range(1, len(piece) - 1):
            theta = heading_change_deg(piece[i - 1], piece[i], piece[i + 1])
            if theta >= 179.0:
                need[i] = math.inf
            elif theta > 1.0:
                need[i] = radius * math.tan(math.radians(theta) / 2)
        for i in range(len(piece) - 1):
            length = math.dist(piece[i], piece[i + 1])
            required = need[i] + need[i + 1]
            if required > length + TOLERANCE_M:
                corner = piece[i + 1] if need[i + 1] >= need[i] else piece[i]
                findings.append(_turn_message(corner, length, required, radius))
    return findings


def _turn_message(corner: Point, length: float, required: float, radius: float) -> str:
    if math.isinf(required):
        return (
            f"reversal near ({corner[0]:.2f}, {corner[1]:.2f}) is impossible with a turning "
            f"radius of {radius:.2f} m"
        )
    return (
        f"turn near ({corner[0]:.2f}, {corner[1]:.2f}) needs {required:.2f} m of straight track "
        f"but has {length:.2f} m (turning radius {radius:.2f} m)"
    )


def findings_summary(reasons: Sequence[str]) -> str:
    """One line for logs."""
    return "; ".join(reasons) if reasons else "valid"
