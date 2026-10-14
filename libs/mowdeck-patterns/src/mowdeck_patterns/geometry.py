"""Geometry building blocks of the patterns: insets, stripe runs, offset rings, transfers.

Patterns only decide *which* runs to cut and in which order (:class:`Run` lists). Turning those
runs into a plan is the same for every pattern (:func:`assemble`): neighbouring runs are joined
by a short ``turn`` with the blades on when the straight connection stays inside the cutting
region; everything else becomes a ``transfer`` with the blades off, routed around holes and
zones along the shortest way inside the safe region (a visibility graph).
"""

import heapq
import itertools
import math
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from shapely import prepare
from shapely.affinity import rotate
from shapely.geometry import LineString, MultiLineString, MultiPolygon, Polygon
from shapely.geometry import Point as ShapelyPoint
from shapely.geometry.base import BaseGeometry

from mowdeck_patterns.models import (
    MowerSpec,
    PlanningError,
    Point,
    Segment,
    polygonal,
    polygons_of,
)

EPS = 1e-6
MIN_RUN_M = 0.02
"""Runs shorter than this are dropped."""
ROUNDING_SLACK_M = 0.01
"""Extra distance patterns keep from the margins, so that rounding the plan to centimetres
(``MowPlan.to_spot_plan``) never moves a point across the validated safety margin."""
SHARP_TURN_DEG = 60.0
"""Heading changes above this count as a turn in the duration estimate."""

Run = tuple[Point, ...]
"""A polyline cut with the blades on, in driving order."""


def inset(region: BaseGeometry, distance: float) -> Polygon | MultiPolygon:
    """``region`` shrunk by ``distance`` with mitred corners (rectangles stay rectangles)."""
    if distance <= 0:
        return polygonal(region)
    return polygonal(region.buffer(-distance, join_style="mitre", mitre_limit=5.0))


@dataclass(frozen=True, slots=True)
class Workspace:
    """The regions of one area for one mower.

    ``region``: what may be mowed. ``safe``: where the mower's centre may be at all (region
    shrunk by the safety margin). ``work``: where the centre runs while cutting (region shrunk by
    half the cutting width, at least the margin).
    """

    region: Polygon | MultiPolygon
    safe: Polygon | MultiPolygon
    work: Polygon | MultiPolygon
    mower: MowerSpec

    @classmethod
    def of(cls, region: Polygon | MultiPolygon, mower: MowerSpec) -> "Workspace":
        """Workspace of ``region``."""
        return cls(
            region=region,
            safe=inset(region, mower.safety_margin_m + ROUNDING_SLACK_M),
            work=inset(region, mower.inset_m + ROUNDING_SLACK_M),
            mower=mower,
        )


# --- Stripes ----------------------------------------------------------------------------------


def stripe_lines(
    region: BaseGeometry, angle_deg: float, spacing: float, min_run: float = MIN_RUN_M
) -> list[list[tuple[float, float, float]]]:
    """Intervals of parallel scan lines through ``region`` at ``angle_deg`` (from +x, CCW).

    Returns one list per scan line (bottom to top in the rotated frame) of ``(y, x0, x1)``
    intervals sorted by ``x0``, all in the rotated frame (rotated by ``-angle_deg`` about the
    origin). The outer lines lie on the region's edge (the blades cut up to the area's border),
    a region thinner than ``spacing`` gets one line in its middle.
    """
    if region.is_empty:
        return []
    rotated = rotate(region, -angle_deg, origin=(0.0, 0.0))
    minx, miny, maxx, maxy = rotated.bounds
    height = maxy - miny
    margin = min(1e-3, height / 4)
    usable = height - 2 * margin
    if usable <= spacing:
        ys = [miny + height / 2]
    else:
        count = math.ceil(usable / spacing - 1e-9) + 1
        step = usable / (count - 1)
        ys = [miny + margin + i * step for i in range(count)]
    prepared = rotated
    prepare(prepared)
    lines: list[list[tuple[float, float, float]]] = []
    for y in ys:
        scan = LineString([(minx - 1.0, y), (maxx + 1.0, y)])
        cut = prepared.intersection(scan)
        intervals: list[tuple[float, float, float]] = []
        for part in _lines_of(cut):
            xs = [c[0] for c in part.coords]
            x0, x1 = min(xs), max(xs)
            if x1 - x0 >= min_run:
                intervals.append((y, x0, x1))
        intervals.sort(key=lambda item: item[1])
        lines.append(intervals)
    return lines


def _lines_of(geometry: BaseGeometry) -> list[LineString]:
    if geometry.is_empty:
        return []
    if isinstance(geometry, LineString):
        return [geometry]
    found: list[LineString] = []
    for part in getattr(geometry, "geoms", ()):
        found.extend(_lines_of(part))
    return found


def boustrophedon(
    lines: Sequence[Sequence[tuple[float, float, float]]], start: Point | None = None
) -> list[tuple[Point, Point]]:
    """Order stripe intervals into back-and-forth runs (rotated frame).

    From the current run the next one is an unvisited interval on the next scan line that
    overlaps it (so the turn is short); when there is none, the nearest unvisited interval
    anywhere (a transfer). The result are ``(start, end)`` pairs in driving order.
    """
    remaining: dict[tuple[int, int], tuple[float, float, float]] = {
        (i, j): interval for i, line in enumerate(lines) for j, interval in enumerate(line)
    }
    if not remaining:
        return []
    order: list[tuple[Point, Point]] = []
    if start is None:
        key = min(remaining, key=lambda k: (k[0], remaining[k][1]))
        forward = True
    else:
        key, forward = _nearest(remaining, start)
    while True:
        y, x0, x1 = remaining.pop(key)
        run = ((x0, y), (x1, y)) if forward else ((x1, y), (x0, y))
        order.append(run)
        if not remaining:
            return order
        line_index = key[0]
        follow = [
            k
            for k in remaining
            if k[0] == line_index + 1 and remaining[k][1] < x1 and remaining[k][2] > x0
        ]
        if follow:
            end_x = run[1][0]
            key = min(
                follow,
                key=lambda k: min(abs(remaining[k][1] - end_x), abs(remaining[k][2] - end_x)),
            )
            forward = not forward
        else:
            key, forward = _nearest(remaining, run[1])


def _nearest(
    remaining: dict[tuple[int, int], tuple[float, float, float]], point: Point
) -> tuple[tuple[int, int], bool]:
    best: tuple[float, tuple[int, int], bool] | None = None
    for k, (y, x0, x1) in remaining.items():
        for forward, x in ((True, x0), (False, x1)):
            d = math.hypot(x - point[0], y - point[1])
            if best is None or d < best[0] - EPS:
                best = (d, k, forward)
    assert best is not None  # noqa: S101 - remaining is not empty
    return best[1], best[2]


def stripe_runs(
    region: BaseGeometry, angle_deg: float, spacing: float, start: Point | None = None
) -> list[Run]:
    """Back-and-forth stripe runs through ``region`` at ``angle_deg``, in the map frame."""
    lines = stripe_lines(region, angle_deg, spacing)
    rotated_start = None
    if start is not None:
        rotated_start = _rotate_point(start, -angle_deg)
    pairs = boustrophedon(lines, rotated_start)
    return [(_rotate_point(a, angle_deg), _rotate_point(b, angle_deg)) for a, b in pairs]


def count_stripe_runs(region: BaseGeometry, angle_deg: float, spacing: float) -> int:
    """Number of runs (and so roughly of turns) stripes at ``angle_deg`` need."""
    return sum(len(line) for line in stripe_lines(region, angle_deg, spacing, min_run=0.0))


def _rotate_point(point: Point, angle_deg: float) -> Point:
    angle = math.radians(angle_deg)
    c, s = math.cos(angle), math.sin(angle)
    return (point[0] * c - point[1] * s, point[0] * s + point[1] * c)


def edge_angles(region: BaseGeometry, limit: int = 24) -> list[float]:
    """Directions (0 to 180 degrees) of the longest edges of ``region``'s exteriors."""
    edges: list[tuple[float, float]] = []
    for polygon in polygons_of(region):
        coords = list(polygon.exterior.coords)
        for a, b in itertools.pairwise(coords):
            length = math.dist(a, b)
            if length > EPS:
                angle = math.degrees(math.atan2(b[1] - a[1], b[0] - a[0])) % 180.0
                edges.append((length, round(angle, 3)))
    edges.sort(reverse=True)
    seen: list[float] = []
    for _, angle in edges:
        if all(min(abs(angle - s), 180 - abs(angle - s)) > 0.5 for s in seen):
            seen.append(angle)
        if len(seen) >= limit:
            break
    return seen


# --- Rings ------------------------------------------------------------------------------------


def offset_levels(
    region: BaseGeometry, spacing: float, max_levels: int | None = None
) -> list[list[Polygon]]:
    """Polygons of ``region`` shrunk by 0, ``spacing``, ``2 * spacing``, ... until empty."""
    levels: list[list[Polygon]] = []
    level = 0
    while max_levels is None or level < max_levels:
        shrunk = inset(region, level * spacing) if level else polygonal(region)
        polygons = [p for p in polygons_of(shrunk) if p.area > MIN_RUN_M**2]
        if not polygons:
            break
        levels.append(polygons)
        level += 1
        if level > 10_000:  # pragma: no cover - defensive
            break
    return levels


def ring_points(coords: Iterable[Sequence[float]]) -> list[Point]:
    """Points of a closed ring without the repeated closing point."""
    points = [(float(c[0]), float(c[1])) for c in coords]
    if len(points) > 1 and points[0] == points[-1]:
        points.pop()
    return points


def closed_lap(ring: Sequence[Point], near: Point | None, *, clockwise: bool | None = None) -> Run:
    """``ring`` as a closed run starting at the vertex nearest ``near``."""
    points = list(ring)
    if clockwise is not None and _is_clockwise(points) != clockwise:
        points.reverse()
    if near is not None and points:
        index = min(range(len(points)), key=lambda i: math.dist(points[i], near))
        points = points[index:] + points[:index]
    return (*points, points[0])


def _is_clockwise(points: Sequence[Point]) -> bool:
    area = sum(
        a[0] * b[1] - b[0] * a[1] for a, b in zip(points, [*points[1:], points[0]], strict=True)
    )
    return area < 0


def nearest_first[T](items: list[T], ends: dict[int, tuple[Point, Point]], start: Point) -> list[T]:
    """Order ``items`` greedily: always the one whose start is nearest the previous end."""
    left = list(range(len(items)))
    order: list[T] = []
    here = start
    while left:
        index = min(left, key=lambda i: math.dist(ends[i][0], here))
        left.remove(index)
        order.append(items[index])
        here = ends[index][1]
    return order


# --- Transfers --------------------------------------------------------------------------------


class Router:
    """Shortest paths inside a polygonal region along a visibility graph of its vertices."""

    def __init__(self, region: Polygon | MultiPolygon) -> None:
        self.region = region
        self._tolerant = region.buffer(1e-4)
        prepare(self._tolerant)
        nodes: list[Point] = []
        core = region.buffer(-0.01, join_style="mitre", mitre_limit=5.0)
        for polygon in polygons_of(core):
            for ring in (polygon.exterior, *polygon.interiors):
                nodes.extend(ring_points(ring.coords))
        self._nodes = nodes
        self._edges: dict[int, list[tuple[int, float]]] | None = None

    def clear(self, a: Point, b: Point) -> bool:
        """True when the straight line from ``a`` to ``b`` stays inside the region."""
        if math.dist(a, b) < EPS:
            return bool(self._tolerant.covers(ShapelyPoint(a)))
        return bool(self._tolerant.covers(LineString([a, b])))

    def _graph(self) -> dict[int, list[tuple[int, float]]]:
        if self._edges is None:
            edges: dict[int, list[tuple[int, float]]] = {i: [] for i in range(len(self._nodes))}
            for i, a in enumerate(self._nodes):
                for j in range(i + 1, len(self._nodes)):
                    b = self._nodes[j]
                    if self.clear(a, b):
                        d = math.dist(a, b)
                        edges[i].append((j, d))
                        edges[j].append((i, d))
            self._edges = edges
        return self._edges

    def route(self, a: Point, b: Point) -> list[Point]:
        """The shortest path from ``a`` to ``b``; raises :class:`PlanningError` without one."""
        if self.clear(a, b):
            return [a, b]
        graph = self._graph()
        count = len(self._nodes)
        start, goal = count, count + 1
        adjacency: dict[int, list[tuple[int, float]]] = {k: list(v) for k, v in graph.items()}
        adjacency[start] = []
        adjacency[goal] = []
        for i, vertex in enumerate(self._nodes):
            if self.clear(a, vertex):
                adjacency[start].append((i, math.dist(a, vertex)))
            if self.clear(vertex, b):
                adjacency[i].append((goal, math.dist(vertex, b)))
        best: dict[int, float] = {start: 0.0}
        previous: dict[int, int] = {}
        queue: list[tuple[float, int]] = [(0.0, start)]
        while queue:
            dist, node = heapq.heappop(queue)
            if node == goal:
                break
            if dist > best.get(node, math.inf):
                continue
            for neighbour, weight in adjacency[node]:
                candidate = dist + weight
                if candidate < best.get(neighbour, math.inf) - EPS:
                    best[neighbour] = candidate
                    previous[neighbour] = node
                    heapq.heappush(queue, (candidate, neighbour))
        if goal not in best:
            msg = (
                f"no way from ({a[0]:.2f}, {a[1]:.2f}) to ({b[0]:.2f}, {b[1]:.2f}) inside the "
                "area: it falls apart into separate parts, plan them separately"
            )
            raise PlanningError(msg)
        path = [b]
        node = goal
        while node != start:
            node = previous[node]
            path.append(a if node == start else self._nodes[node])
        path.reverse()
        return path


# --- Assembling -------------------------------------------------------------------------------


def assemble(
    runs: Sequence[Run],
    workspace: Workspace,
    *,
    speed: float | None = None,
    router: Router | None = None,
    start: Point | None = None,
) -> list[Segment]:
    """Turn ordered runs into segments: runs, turns between neighbours, routed transfers."""
    mower = workspace.mower
    mow_speed = min(speed or mower.max_speed_mps, mower.max_speed_mps)
    turn_limit = max(3 * mower.spacing_m, 1.0)
    cutting = workspace.work.buffer(1e-4)
    prepare(cutting)
    active_router = router or Router(workspace.safe)
    segments: list[Segment] = []
    here = start
    for run in runs:
        points = _clean(run)
        if len(points) < 2:
            continue
        if here is not None and math.dist(here, points[0]) > EPS:
            gap = math.dist(here, points[0])
            joint = LineString([here, points[0]])
            if gap <= turn_limit and cutting.covers(joint):
                segments.append(Segment((here, points[0]), True, mow_speed, "turn"))
            else:
                path = active_router.route(here, points[0])
                segments.append(Segment(tuple(path), False, mower.transfer_speed_mps, "transfer"))
        segments.append(Segment(tuple(points), True, mow_speed, "mow"))
        here = points[-1]
    return segments


def _clean(run: Sequence[Point]) -> list[Point]:
    points: list[Point] = []
    for point in run:
        if not points or math.dist(points[-1], point) > EPS:
            points.append(point)
    return points


def join(first: list[Segment], second: list[Segment], workspace: Workspace) -> list[Segment]:
    """Concatenate two segment lists with a blades-off transfer between them if needed."""
    if not first:
        return list(second)
    if not second:
        return list(first)
    end, start = first[-1].end, second[0].start
    if math.dist(end, start) <= EPS:
        return [*first, *second]
    path = Router(workspace.safe).route(end, start)
    transfer = Segment(tuple(path), False, workspace.mower.transfer_speed_mps, "transfer")
    return [*first, transfer, *second]


# --- Estimates --------------------------------------------------------------------------------


def covered_geometry(segments: Sequence[Segment], width: float) -> BaseGeometry:
    """The ground the blades pass over: every blades-on segment widened to ``width``."""
    lines = [s.polyline for s in segments if s.blades_on]
    if not lines:
        return Polygon()
    return MultiLineString([list(line) for line in lines]).buffer(width / 2, quad_segs=4)


def sharp_turns(segments: Sequence[Segment]) -> int:
    """Number of heading changes above :data:`SHARP_TURN_DEG` along the whole plan."""
    points: list[Point] = []
    for segment in segments:
        for point in segment.polyline:
            if not points or math.dist(points[-1], point) > EPS:
                points.append(point)
    count = 0
    for a, b, c in zip(points, points[1:], points[2:], strict=False):
        if heading_change_deg(a, b, c) > SHARP_TURN_DEG:
            count += 1
    return count


def heading_change_deg(a: Point, b: Point, c: Point) -> float:
    """Absolute heading change at ``b`` driving ``a`` to ``b`` to ``c`` (0 to 180 degrees)."""
    h1 = math.atan2(b[1] - a[1], b[0] - a[0])
    h2 = math.atan2(c[1] - b[1], c[0] - b[0])
    diff = abs(h2 - h1) % (2 * math.pi)
    return math.degrees(min(diff, 2 * math.pi - diff))


def duration_s(segments: Sequence[Segment], mower: MowerSpec) -> float:
    """Driving time plus :attr:`MowerSpec.turn_time_s` per sharp turn."""
    driving = sum(s.length / s.speed for s in segments)
    return driving + sharp_turns(segments) * mower.turn_time_s
