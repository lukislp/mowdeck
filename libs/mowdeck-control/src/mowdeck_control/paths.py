"""A simple boustrophedon (back and forth) stripe path for the simulated mower.

The path stays ``margin`` metres inside the area and away from forbidden zones. Every leg is
checked by sampling (:func:`~mowdeck_control.geo.segment_is_clear`); a stripe that cannot be
reached without leaving the area or touching a forbidden zone is skipped. So the path is always
safe, but may leave gaps around zones in the middle of an area. That is good enough for a
simulator; real planning belongs to ``mowdeck-patterns``.
"""

import math
from collections.abc import Sequence

from mowdeck_contracts.v1 import LocalPoint

from mowdeck_control.geo import rotate, scanline_intervals, segment_is_clear

Interval = tuple[float, float]
_EPS = 1e-9


def _subtract(intervals: list[Interval], blocked: list[Interval]) -> list[Interval]:
    result = intervals
    for b_start, b_end in blocked:
        next_result: list[Interval] = []
        for start, end in result:
            if b_end <= start or b_start >= end:
                next_result.append((start, end))
                continue
            if b_start > start:
                next_result.append((start, b_start))
            if b_end < end:
                next_result.append((b_end, end))
        result = next_result
    return [(s, e) for s, e in result if e - s > _EPS]


def stripe_path(
    area: Sequence[LocalPoint],
    forbidden: Sequence[Sequence[LocalPoint]] = (),
    *,
    spacing_m: float,
    angle_deg: float = 0.0,
    margin_m: float = 0.2,
) -> list[LocalPoint]:
    """Return the waypoints of a stripe path at ``angle_deg`` (counter-clockwise from +x).

    Consecutive waypoints are connected by straight legs, all inside ``area`` and outside every
    ``forbidden`` polygon.
    """
    if spacing_m <= 0 or margin_m < 0:
        msg = "spacing_m must be positive and margin_m not negative"
        raise ValueError(msg)
    angle = math.radians(angle_deg)
    rot_area = [rotate(p, -angle) for p in area]
    rot_zones = [[rotate(p, -angle) for p in zone] for zone in forbidden]

    ys = [p[1] for p in rot_area]
    y = min(ys) + margin_m
    y_max = max(ys) - margin_m
    lines: list[tuple[float, list[Interval]]] = []
    while y <= y_max + _EPS:
        intervals = [
            (s + margin_m, e - margin_m)
            for s, e in scanline_intervals(rot_area, y)
            if e - s > 2 * margin_m
        ]
        for zone in rot_zones:
            blocked: list[Interval] = []
            for dy in (-margin_m, 0.0, margin_m):
                blocked.extend(
                    (s - margin_m, e + margin_m) for s, e in scanline_intervals(zone, y + dy)
                )
            intervals = _subtract(intervals, blocked)
        lines.append((y, intervals))
        y += spacing_m

    def clear(a: LocalPoint, b: LocalPoint) -> bool:
        return segment_is_clear(a, b, inside=rot_area, outside=rot_zones)

    points: list[LocalPoint] = []
    current: LocalPoint | None = None
    leftward = False
    for line_y, intervals in lines:
        if not intervals:
            continue
        for s, e in sorted(intervals, reverse=leftward):
            start, end = ((e, line_y), (s, line_y)) if leftward else ((s, line_y), (e, line_y))
            if current is None:
                points.append(start)
            else:
                corner = (start[0], current[1])
                if clear(current, corner) and clear(corner, start):
                    points.extend([corner, start])
                elif clear(current, start):
                    points.append(start)
                else:
                    continue  # unreachable without crossing a zone: skip this stripe
            points.append(end)
            current = end
        leftward = not leftward

    deduped: list[LocalPoint] = []
    for p in points:
        if not deduped or math.dist(deduped[-1], p) > _EPS:
            deduped.append(p)
    return [rotate(p, angle) for p in deduped]
