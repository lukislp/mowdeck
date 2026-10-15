"""Transit routes for spot runs (ADR 0041): from where the mower stands to the start of a spot,
on the mower's neutral map, without I/O.

A spot run that starts at the charging station, or far from the spot, first drives a *transit*
with the blades off. :func:`plan_transit` finds it on a graph built from the map:

* **Drivable space** (:class:`DrivableSpace`): the mapped areas inset by ``area_margin_m``, the
  transfer paths widened to corridors of ``path_half_width_m`` on each side and a disc of
  ``dock_apron_m`` around the dock (where an undocked mower stands), minus every no-go zone and
  obstacle buffered by ``zone_margin_m``. The run's geofence during the transit is this space.
* **Nodes**: the start and the goal, the path vertices (simplified, then at most
  ``path_spacing_m`` apart), the concave corners of every area and the convex corners of every
  zone, pushed ``node_clearance_m`` into the free side, and *portals*: for a node near an area
  but outside its inset (a path end on the lawn's edge, a mower in front of the dock), the
  nearest point inside the inset.
* **Edges**: consecutive path vertices (the corridor's centre line); straight connections inside
  one area between its corners, portals, path entries, the start and the goal (exact test: the
  segment keeps ``area_margin_m`` from the area's edge and ``zone_margin_m`` from every zone);
  and short *connectors* of at most ``connect_max_m`` between any two nodes that stay inside the
  drivable space (sampled every 5 cm): a path end to its portal, two paths that meet, the start
  to a nearby path.
* **Search**: A* (Dijkstra with the straight-line distance as a lower bound) with lazily checked
  edges, so a route can chain several paths and pass through intermediate areas. No route, or
  one longer than ``max_length_m``, raises :class:`NoRouteError`.

Everything is pure Python on ``mowdeck-contracts`` (no geometry library): the Planner computes
the same route for the preview as the Device-Gateway's run, from the same map and start.
"""

import heapq
import itertools
import math
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import Final

from mowdeck_contracts.v1 import LocalPoint, MowerMap

from mowdeck_control.geo import (
    boundary_distance,
    distance,
    point_in_polygon,
    segment_distance,
)

REASON_NO_ROUTE: Final = "no_route"
"""``SpotRunStatus.reason`` of a run without a transit route (and the prefix of the rejection)."""


class NoRouteError(ValueError):
    """No drivable route from the start to the goal (the message says why)."""


@dataclass(frozen=True, slots=True)
class TransitConfig:
    """Geometry of the transit planner (the defaults are the design values)."""

    area_margin_m: float = 0.3
    """Distance kept from the edge of an area (the spot run's geofence margin)."""
    zone_margin_m: float = 0.3
    """Distance kept from every no-go zone and obstacle."""
    path_half_width_m: float = 0.5
    """Half width of a transfer path's corridor."""
    dock_apron_m: float = 1.5
    """Radius of the drivable disc around the dock (where an undocked mower stands)."""
    node_clearance_m: float = 0.2
    """How far corner and portal nodes lie inside the free side, beyond the margins."""
    connect_max_m: float = 2.0
    """Longest connector between two nodes outside a common area."""
    path_spacing_m: float = 1.0
    """Longest distance between two path vertices after densifying."""
    simplify_m: float = 0.05
    """Tolerance of the polyline simplification (paths and area outlines)."""
    sample_m: float = 0.05
    """Sample distance of a connector's drivable-space check."""
    max_length_m: float = 300.0
    """Longest transit route; a longer one counts as no route."""

    def __post_init__(self) -> None:
        if min(self.area_margin_m, self.zone_margin_m) < 0:
            msg = "margins must not be negative"
            raise ValueError(msg)
        if min(self.path_half_width_m, self.connect_max_m, self.path_spacing_m) <= 0:
            msg = "path_half_width_m, connect_max_m and path_spacing_m must be positive"
            raise ValueError(msg)
        if self.sample_m <= 0 or self.max_length_m <= 0:
            msg = "sample_m and max_length_m must be positive"
            raise ValueError(msg)


@dataclass(frozen=True, slots=True)
class TransitRoute:
    """A transit: the polyline from the start to the goal (both included)."""

    points: tuple[LocalPoint, ...]
    length_m: float

    @property
    def is_trivial(self) -> bool:
        """True when start and goal coincide (nothing to drive)."""
        return len(self.points) < 2 or self.length_m < 1e-6


# --- geometry -----------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Box:
    x0: float
    y0: float
    x1: float
    y1: float

    @classmethod
    def of(cls, points: Iterable[LocalPoint], pad: float = 0.0) -> "_Box":
        xs, ys = zip(*points, strict=True)
        return cls(min(xs) - pad, min(ys) - pad, max(xs) + pad, max(ys) + pad)

    def contains(self, p: LocalPoint) -> bool:
        return self.x0 <= p[0] <= self.x1 and self.y0 <= p[1] <= self.y1

    def meets(self, a: LocalPoint, b: LocalPoint) -> bool:
        return not (
            max(a[0], b[0]) < self.x0
            or min(a[0], b[0]) > self.x1
            or max(a[1], b[1]) < self.y0
            or min(a[1], b[1]) > self.y1
        )


@dataclass(frozen=True, slots=True)
class _Ring:
    points: tuple[LocalPoint, ...]
    box: _Box

    @classmethod
    def of(cls, points: Sequence[LocalPoint], pad: float) -> "_Ring":
        return cls(tuple(points), _Box.of(points, pad))

    def edges(self) -> Iterable[tuple[LocalPoint, LocalPoint]]:
        pts = self.points
        return zip(pts, pts[1:] + pts[:1], strict=True)


def _cross(o: LocalPoint, a: LocalPoint, b: LocalPoint) -> float:
    return (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0])


def _segments_intersect(a: LocalPoint, b: LocalPoint, c: LocalPoint, d: LocalPoint) -> bool:
    d1, d2 = _cross(c, d, a), _cross(c, d, b)
    d3, d4 = _cross(a, b, c), _cross(a, b, d)
    return d1 * d2 < 0 and d3 * d4 < 0


def segment_segment_distance(a: LocalPoint, b: LocalPoint, c: LocalPoint, d: LocalPoint) -> float:
    """Shortest distance between the segments ``a``-``b`` and ``c``-``d`` (0 when they cross)."""
    if _segments_intersect(a, b, c, d):
        return 0.0
    return min(
        segment_distance(a, c, d),
        segment_distance(b, c, d),
        segment_distance(c, a, b),
        segment_distance(d, a, b),
    )


def _signed_area(ring: Sequence[LocalPoint]) -> float:
    n = len(ring)
    return 0.5 * sum(
        ring[i][0] * ring[(i + 1) % n][1] - ring[(i + 1) % n][0] * ring[i][1] for i in range(n)
    )


def simplify(points: Sequence[LocalPoint], tolerance: float) -> list[LocalPoint]:
    """Douglas-Peucker simplification of an open polyline (end points kept)."""
    if len(points) < 3:
        return list(points)
    keep = [False] * len(points)
    keep[0] = keep[-1] = True
    stack = [(0, len(points) - 1)]
    while stack:
        first, last = stack.pop()
        best, index = 0.0, -1
        for i in range(first + 1, last):
            d = segment_distance(points[i], points[first], points[last])
            if d > best:
                best, index = d, i
        if index >= 0 and best > tolerance:
            keep[index] = True
            stack.append((first, index))
            stack.append((index, last))
    return [p for p, k in zip(points, keep, strict=True) if k]


def _simplify_ring(ring: Sequence[LocalPoint], tolerance: float) -> list[LocalPoint]:
    if len(ring) <= 4:
        return list(ring)
    far = max(range(len(ring)), key=lambda i: distance(ring[0], ring[i]))
    first = simplify([*ring[: far + 1]], tolerance)
    second = simplify([*ring[far:], ring[0]], tolerance)
    out = first[:-1] + second[:-1]
    return out if len(out) >= 3 else list(ring)


def densify(points: Sequence[LocalPoint], spacing: float) -> list[LocalPoint]:
    """``points`` with extra vertices so that no edge is longer than ``spacing``."""
    out: list[LocalPoint] = [points[0]]
    for a, b in itertools.pairwise(points):
        steps = max(1, math.ceil(distance(a, b) / spacing))
        out.extend(
            (a[0] + (b[0] - a[0]) * i / steps, a[1] + (b[1] - a[1]) * i / steps)
            for i in range(1, steps + 1)
        )
    return out


def _unit(dx: float, dy: float) -> tuple[float, float] | None:
    length = math.hypot(dx, dy)
    if length < 1e-12:
        return None
    return dx / length, dy / length


# --- the drivable space -------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class DrivableSpace:
    """Where a transit may drive (see the module docs)."""

    areas: tuple[_Ring, ...]
    zones: tuple[_Ring, ...]
    paths: tuple[tuple[LocalPoint, ...], ...]
    """The transfer paths, simplified by ``simplify_m``."""
    path_boxes: tuple[_Box, ...]
    dock: LocalPoint | None
    config: TransitConfig = field(default_factory=TransitConfig)

    @classmethod
    def of(cls, mower_map: MowerMap, config: TransitConfig | None = None) -> "DrivableSpace":
        """The drivable space of ``mower_map``."""
        config = config or TransitConfig()
        paths = tuple(tuple(simplify(p.points, config.simplify_m)) for p in mower_map.paths)
        return cls(
            areas=tuple(_Ring.of(a.boundary, 0.0) for a in mower_map.areas),
            zones=tuple(
                _Ring.of(z.boundary, config.zone_margin_m) for z in mower_map.forbidden_zones()
            ),
            paths=paths,
            path_boxes=tuple(_Box.of(p, config.path_half_width_m) for p in paths),
            dock=(mower_map.dock.x, mower_map.dock.y) if mower_map.dock is not None else None,
            config=config,
        )

    # --- points --------------------------------------------------------------------------------

    def in_zone_margin(self, p: LocalPoint) -> bool:
        """True when ``p`` lies in a zone or within ``zone_margin_m`` of one."""
        margin = self.config.zone_margin_m
        return any(
            zone.box.contains(p)
            and (point_in_polygon(p, zone.points) or boundary_distance(p, zone.points) < margin)
            for zone in self.zones
        )

    def area_index(self, p: LocalPoint) -> int | None:
        """The first area whose inset holds ``p`` (zones not considered), or None."""
        for index in self.areas_of(p):
            return index
        return None

    def areas_of(self, p: LocalPoint) -> Iterable[int]:
        """Every area whose inset holds ``p`` (zones not considered)."""
        margin = self.config.area_margin_m
        for index, area in enumerate(self.areas):
            if (
                area.box.contains(p)
                and point_in_polygon(p, area.points)
                and boundary_distance(p, area.points) >= margin
            ):
                yield index

    def in_corridor(self, p: LocalPoint) -> bool:
        """True when ``p`` lies within ``path_half_width_m`` of a transfer path."""
        width = self.config.path_half_width_m
        for path, box in zip(self.paths, self.path_boxes, strict=True):
            if not box.contains(p):
                continue
            for a, b in itertools.pairwise(path):
                if segment_distance(p, a, b) <= width:
                    return True
        return False

    def in_apron(self, p: LocalPoint) -> bool:
        """True when ``p`` lies within ``dock_apron_m`` of the dock."""
        return self.dock is not None and distance(p, self.dock) <= self.config.dock_apron_m

    def violation(self, p: LocalPoint) -> str | None:
        """None when ``p`` lies in the drivable space, else what is wrong."""
        if self.in_zone_margin(p):
            return f"within {self.config.zone_margin_m:g} m of a no-go zone or obstacle"
        if self.area_index(p) is not None or self.in_corridor(p) or self.in_apron(p):
            return None
        return "outside the mapped areas, the transfer paths and the dock apron"

    def contains(self, p: LocalPoint) -> bool:
        """True when ``p`` lies in the drivable space."""
        return self.violation(p) is None

    # --- segments ------------------------------------------------------------------------------

    def segment_clear_sampled(self, a: LocalPoint, b: LocalPoint) -> bool:
        """Every sample of ``a``-``b`` (every ``sample_m``) lies in the drivable space."""
        steps = max(1, math.ceil(distance(a, b) / self.config.sample_m))
        dx, dy = b[0] - a[0], b[1] - a[1]
        return all(
            self.contains((a[0] + dx * i / steps, a[1] + dy * i / steps)) for i in range(steps + 1)
        )

    def clear_of_zones(self, a: LocalPoint, b: LocalPoint) -> bool:
        """Exact: ``a``-``b`` keeps ``zone_margin_m`` from every zone."""
        margin = self.config.zone_margin_m
        for zone in self.zones:
            if not zone.box.meets(a, b):
                continue
            if point_in_polygon(a, zone.points):
                return False
            for c, d in zone.edges():
                if segment_segment_distance(a, b, c, d) < margin:
                    return False
        return True

    def inside_area(self, index: int, a: LocalPoint, b: LocalPoint) -> bool:
        """Exact: ``a``-``b`` lies in the inset of area ``index`` and clear of every zone."""
        area = self.areas[index]
        if not point_in_polygon(a, area.points):
            return False
        margin = self.config.area_margin_m
        for c, d in area.edges():
            if segment_segment_distance(a, b, c, d) < margin:
                return False
        return self.clear_of_zones(a, b)


# --- the graph ----------------------------------------------------------------------------------

_START: Final = 0
_GOAL: Final = 1


@dataclass(slots=True)
class _Node:
    point: LocalPoint
    areas: frozenset[int]
    area_node: bool
    """Takes part in the straight connections inside its areas."""
    path: int | None = None
    path_index: int = -1


class _Graph:
    def __init__(self, space: DrivableSpace, start: LocalPoint, goal: LocalPoint) -> None:
        self.space = space
        self.config = space.config
        self.nodes: list[_Node] = []
        self.path_nodes: list[list[int]] = []
        self.area_nodes: dict[int, list[int]] = {}
        self._edge_cache: dict[tuple[int, int, str], bool] = {}
        self._cell = self.config.connect_max_m
        self._grid: dict[tuple[int, int], list[int]] = {}
        self._add(start, area_node=True)
        self._add(goal, area_node=True)
        self._add_paths()
        self._add_corners()
        for index in [_START, _GOAL, *self._entries()]:
            self._add_portals(index)

    # --- construction ------------------------------------------------------------------------

    def _entries(self) -> list[int]:
        return [
            index
            for indices in self.path_nodes
            for position, index in enumerate(indices)
            if self._path_entry(index, indices, position)
        ]

    def _add(
        self,
        point: LocalPoint,
        *,
        area_node: bool,
        path: int | None = None,
        path_index: int = -1,
    ) -> int:
        areas = frozenset(self.space.areas_of(point))
        index = len(self.nodes)
        self.nodes.append(_Node(point, areas, area_node and bool(areas), path, path_index))
        if area_node:
            for area in areas:
                self.area_nodes.setdefault(area, []).append(index)
        self._grid.setdefault(self._key(point), []).append(index)
        return index

    def _key(self, p: LocalPoint) -> tuple[int, int]:
        return (math.floor(p[0] / self._cell), math.floor(p[1] / self._cell))

    def _add_paths(self) -> None:
        config = self.config
        for path_no, raw in enumerate(self.space.paths):
            points = densify(raw, config.path_spacing_m)
            indices = []
            for i, point in enumerate(points):
                indices.append(self._add(point, area_node=False, path=path_no, path_index=i))
            self.path_nodes.append(indices)
        # Path vertices where the path enters or leaves an area join the area's connections.
        for indices in self.path_nodes:
            for position, index in enumerate(indices):
                if self._path_entry(index, indices, position):
                    node = self.nodes[index]
                    if node.areas and not node.area_node:
                        node.area_node = True
                        for area in node.areas:
                            self.area_nodes.setdefault(area, []).append(index)

    def _path_entry(
        self, index: int, indices: list[int] | None = None, position: int | None = None
    ) -> bool:
        node = self.nodes[index]
        if node.path is None:
            return False
        indices = indices if indices is not None else self.path_nodes[node.path]
        position = position if position is not None else node.path_index
        if position in (0, len(indices) - 1):
            return True
        before = self.nodes[indices[position - 1]].areas
        after = self.nodes[indices[position + 1]].areas
        return before != node.areas or after != node.areas

    def _add_corners(self) -> None:
        config = self.config
        space = self.space
        push = config.node_clearance_m
        for area in space.areas:
            ring = _simplify_ring(area.points, config.simplify_m)
            ccw = _signed_area(ring) > 0
            for point, inward in _corners(ring, ccw=ccw, reflex=True):
                self._add_pushed(point, inward, config.area_margin_m + push)
        for zone in space.zones:
            ring = _simplify_ring(zone.points, config.simplify_m)
            ccw = _signed_area(ring) > 0
            for point, inward in _corners(ring, ccw=ccw, reflex=False):
                outward = (-inward[0], -inward[1])
                self._add_pushed(point, outward, config.zone_margin_m + push)

    def _add_pushed(self, point: LocalPoint, direction: tuple[float, float], offset: float) -> None:
        for factor in (1.0, 1.5, 2.5):
            candidate = (
                point[0] + direction[0] * offset * factor,
                point[1] + direction[1] * offset * factor,
            )
            inside = next(iter(self.space.areas_of(candidate)), None) is not None
            if inside and not self.space.in_zone_margin(candidate):
                self._add(candidate, area_node=True)
                return

    def _add_portals(self, index: int) -> None:
        """Points inside the inset of every nearby area the node is not inside of yet."""
        node = self.nodes[index]
        config = self.config
        reach = config.connect_max_m - 0.05
        depth = config.area_margin_m + config.node_clearance_m
        for area_no, area in enumerate(self.space.areas):
            if area_no in node.areas:
                continue
            if not area.box.contains(node.point) and not _Box(
                area.box.x0 - reach, area.box.y0 - reach, area.box.x1 + reach, area.box.y1 + reach
            ).contains(node.point):
                continue
            best: tuple[float, LocalPoint, LocalPoint, LocalPoint] | None = None
            for c, d in area.edges():
                dist = segment_distance(node.point, c, d)
                if best is None or dist < best[0]:
                    best = (dist, _closest(node.point, c, d), c, d)
            if best is None or best[0] > reach:
                continue
            _, foot, c, d = best
            normal = _unit(-(d[1] - c[1]), d[0] - c[0])
            if normal is None:
                continue
            for sign in (1.0, -1.0):
                candidate = (foot[0] + sign * normal[0] * depth, foot[1] + sign * normal[1] * depth)
                if (
                    area_no in set(self.space.areas_of(candidate))
                    and not self.space.in_zone_margin(candidate)
                    and distance(candidate, node.point) <= config.connect_max_m
                ):
                    self._add(candidate, area_node=True)
                    break

    # --- edges ----------------------------------------------------------------------------------

    def neighbours(self, index: int) -> Iterable[int]:
        node = self.nodes[index]
        seen: set[int] = {index}
        if node.path is not None:
            indices = self.path_nodes[node.path]
            for position in (node.path_index - 1, node.path_index + 1):
                if 0 <= position < len(indices):
                    other = indices[position]
                    seen.add(other)
                    if self._valid(index, other, "path"):
                        yield other
        if node.area_node:
            for area in node.areas:
                for other in self.area_nodes.get(area, ()):
                    if other in seen:
                        continue
                    seen.add(other)
                    if self._valid(index, other, f"area:{area}"):
                        yield other
        cx, cy = self._key(node.point)
        for gx in (cx - 1, cx, cx + 1):
            for gy in (cy - 1, cy, cy + 1):
                for other in self._grid.get((gx, gy), ()):
                    if other in seen:
                        continue
                    seen.add(other)
                    if self._valid(index, other, "connector"):
                        yield other

    def _valid(self, i: int, j: int, kind: str) -> bool:
        key = (min(i, j), max(i, j), kind)
        cached = self._edge_cache.get(key)
        if cached is not None:
            return cached
        a, b = self.nodes[i].point, self.nodes[j].point
        space = self.space
        if kind == "path":
            ok = space.clear_of_zones(a, b)
        elif kind.startswith("area:"):
            ok = space.inside_area(int(kind[5:]), a, b)
        else:
            same_path = self.nodes[i].path is not None and self.nodes[i].path == self.nodes[j].path
            ok = (
                not same_path
                and distance(a, b) <= self.config.connect_max_m
                and space.segment_clear_sampled(a, b)
            )
        self._edge_cache[key] = ok
        return ok


def _closest(p: LocalPoint, a: LocalPoint, b: LocalPoint) -> LocalPoint:
    dx, dy = b[0] - a[0], b[1] - a[1]
    length = dx * dx + dy * dy
    if length == 0:
        return a
    t = max(0.0, min(1.0, ((p[0] - a[0]) * dx + (p[1] - a[1]) * dy) / length))
    return (a[0] + t * dx, a[1] + t * dy)


def _corners(
    ring: Sequence[LocalPoint], *, ccw: bool, reflex: bool
) -> Iterable[tuple[LocalPoint, tuple[float, float]]]:
    """Corners of ``ring`` with the unit direction into the polygon: the concave ones
    (``reflex``) or the convex ones, ignoring turns below 10 degrees."""
    n = len(ring)
    sign = 1.0 if ccw else -1.0
    for i in range(n):
        prev, cur, nxt = ring[i - 1], ring[i], ring[(i + 1) % n]
        e1 = _unit(cur[0] - prev[0], cur[1] - prev[1])
        e2 = _unit(nxt[0] - cur[0], nxt[1] - cur[1])
        if e1 is None or e2 is None:
            continue
        turn = sign * (e1[0] * e2[1] - e1[1] * e2[0])
        if abs(turn) < math.sin(math.radians(10)):
            continue
        if (turn < 0) != reflex:
            continue
        # inward normals of both edges (left of the direction of travel for a ccw ring)
        n1 = (-e1[1] * sign, e1[0] * sign)
        n2 = (-e2[1] * sign, e2[0] * sign)
        inward = _unit(n1[0] + n2[0], n1[1] + n2[1])
        if inward is not None:
            yield cur, inward


# --- the search ---------------------------------------------------------------------------------


def plan_transit(
    mower_map: MowerMap,
    start: LocalPoint,
    goal: LocalPoint,
    config: TransitConfig | None = None,
) -> TransitRoute:
    """The shortest transit from ``start`` to ``goal`` on ``mower_map`` (see the module docs).

    Raises :class:`NoRouteError` when the start or the goal lies outside the drivable space, when no
    route exists or when the shortest one is longer than ``max_length_m``.
    """
    space = DrivableSpace.of(mower_map, config)
    config = space.config
    problem = space.violation(start)
    if problem is not None:
        raise NoRouteError(f"the mower stands {problem}")
    problem = space.violation(goal)
    if problem is not None:
        raise NoRouteError(f"the start of the spot lies {problem}")
    if distance(start, goal) < 1e-6:
        return TransitRoute((start, goal), 0.0)
    graph = _Graph(space, start, goal)
    nodes = graph.nodes
    best: dict[int, float] = {_START: 0.0}
    previous: dict[int, int] = {}
    queue: list[tuple[float, float, int]] = [(distance(start, goal), 0.0, _START)]
    done: set[int] = set()
    too_long = False
    while queue:
        _, cost, index = heapq.heappop(queue)
        if index in done:
            continue
        if index == _GOAL:
            break
        done.add(index)
        if cost > config.max_length_m:
            too_long = True
            break
        for other in graph.neighbours(index):
            if other in done:
                continue
            step = distance(nodes[index].point, nodes[other].point)
            candidate = cost + step
            if candidate < best.get(other, math.inf):
                best[other] = candidate
                previous[other] = index
                estimate = candidate + distance(nodes[other].point, goal)
                heapq.heappush(queue, (estimate, candidate, other))
    if _GOAL not in best and too_long:
        limit = config.max_length_m
        raise NoRouteError(f"every route is longer than the limit of {limit:g} m")
    if _GOAL not in best:
        raise NoRouteError("no drivable route from the mower to the spot on the map")
    length = best[_GOAL]
    if length > config.max_length_m:
        limit = config.max_length_m
        msg = f"the route is {length:.0f} m long, longer than the limit of {limit:g} m"
        raise NoRouteError(msg)
    chain = [_GOAL]
    while chain[-1] != _START:
        chain.append(previous[chain[-1]])
    points: list[LocalPoint] = []
    for index in reversed(chain):
        point = nodes[index].point
        if not points or distance(points[-1], point) > 1e-6:
            points.append(point)
    if len(points) == 1:
        points.append(goal)
    return TransitRoute(tuple(simplify(points, 0.01)), length)


__all__ = [
    "REASON_NO_ROUTE",
    "DrivableSpace",
    "NoRouteError",
    "TransitConfig",
    "TransitRoute",
    "densify",
    "plan_transit",
    "segment_segment_distance",
    "simplify",
]
