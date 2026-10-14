"""Coverage from a driven track and the gaps left in an area (for re-mowing).

:func:`coverage` widens the track to the cutting width. Jumps longer than ``max_jump_m``
(position dropouts, the mower lifted and carried) break the track instead of counting the
straight line between both points as mowed. :func:`gaps` returns what of the area is left,
without slivers narrower than ``min_width_m`` and pieces smaller than ``min_area_m2``.

The track has no blade state: everything driven in the time range counts as mowed. Callers
choose the range of the job, so transfers to the area are the only false positives.
"""

import math
from collections.abc import Iterable, Sequence

from shapely.geometry import LineString, MultiLineString, Point, Polygon
from shapely.geometry.base import BaseGeometry
from shapely.ops import unary_union

from mowdeck_patterns.models import Area, PatternError, polygonal, polygons_of

DEFAULT_MAX_JUMP_M = 3.0
MAX_TRACK_POINTS = 200_000


def coverage(
    positions: Iterable[Sequence[float]],
    cutting_width: float,
    *,
    max_jump_m: float = DEFAULT_MAX_JUMP_M,
) -> BaseGeometry:
    """The ground covered by a track of local ``(x, y)`` points, widened to ``cutting_width``."""
    if cutting_width <= 0:
        msg = "cutting_width must be positive"
        raise PatternError(msg)
    pieces: list[list[tuple[float, float]]] = []
    current: list[tuple[float, float]] = []
    count = 0
    for raw in positions:
        count += 1
        if count > MAX_TRACK_POINTS:
            msg = f"the track has more than {MAX_TRACK_POINTS} points"
            raise PatternError(msg)
        point = (float(raw[0]), float(raw[1]))
        if not (math.isfinite(point[0]) and math.isfinite(point[1])):
            continue
        if current and math.dist(current[-1], point) > max_jump_m:
            pieces.append(current)
            current = []
        if not current or current[-1] != point:
            current.append(point)
    if current:
        pieces.append(current)
    radius = cutting_width / 2
    lines = [piece for piece in pieces if len(piece) >= 2]
    dots = [Point(piece[0]).buffer(radius, quad_segs=4) for piece in pieces if len(piece) == 1]
    parts: list[BaseGeometry] = list(dots)
    if lines:
        parts.append(MultiLineString(lines).buffer(radius, quad_segs=4))
    if not parts:
        return Polygon()
    return unary_union(parts)


def gaps(
    area: Area | BaseGeometry,
    covered: BaseGeometry,
    min_area: float = 0.05,
    *,
    min_width_m: float = 0.05,
) -> list[Polygon]:
    """Pieces of ``area`` (an :class:`Area` or a polygon) that ``covered`` misses.

    Slivers narrower than ``min_width_m`` are removed (a morphological opening), then pieces
    below ``min_area`` square metres; the largest piece comes first.
    """
    region = area.region() if isinstance(area, Area) else polygonal(area)
    left = region.difference(covered) if not covered.is_empty else region
    if min_width_m > 0 and not left.is_empty:
        half = min_width_m / 2
        left = left.buffer(-half, join_style="mitre").buffer(half, join_style="mitre")
        left = left.intersection(region)
    found = [p for p in polygons_of(left) if p.area >= min_area]
    found.sort(key=lambda p: p.area, reverse=True)
    return found


def covered_ratio(area: Area, covered: BaseGeometry) -> float:
    """Share of the area's mowable region inside ``covered``."""
    region = area.region()
    if region.area <= 0:
        return 0.0
    return float(region.intersection(covered).area / region.area)


def track_line(positions: Sequence[Sequence[float]]) -> LineString | None:
    """The track as one line (None with fewer than two points), for display."""
    points = [(float(p[0]), float(p[1])) for p in positions]
    return LineString(points) if len(points) >= 2 else None
