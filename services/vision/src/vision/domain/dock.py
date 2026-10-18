"""Return to dock as a recovery option (pure, manufacturer independent).

The generic ``return_to_dock`` command lets the mower drive home on its own mapped areas and
transfer paths; it is the lowest-risk way out when the mower stands in a mapped area that is
connected to the dock and no safety stop is active. This module decides from the generic map
model (areas, paths, dock pose) and the mower state whether that holds:

* **Mower area**: the area that contains the mower, else one within :data:`PATH_JOIN_M` (RTK
  noise at the edge); without one, the areas of a transfer path the mower stands on.
* **Dock area**: an area that contains the dock or whose boundary is within
  :data:`DOCK_AREA_MAX_M` of it (docks stand at the lawn's edge, often just outside it); a path
  that ends near the dock (within :data:`DOCK_AREA_MAX_M`) links the dock to its areas too.
* **Connected**: the mower area is a dock area, or a chain of transfer paths links them (a
  path links every area it starts in, ends in or passes through).
* **Blocked**: a lifted mower, an error that is not drive recoverable (lift, tilt and collision
  stops), an error that names a lift, tilt, blade or motor fault, a mower that is docked,
  charging, already returning, offline or updating, or one inside a no-go zone or obstacle.
"""

import itertools
from collections import deque
from collections.abc import Sequence
from typing import Final

from mowdeck_contracts.v1 import LocalPoint, MapArea, MowerActivity, MowerMap, MowerState
from mowdeck_control.geo import boundary_distance, distance, point_in_polygon, segment_distance
from pydantic import BaseModel, Field

DOCK_AREA_MAX_M: Final = 3.0
"""A dock this close to an area's boundary (or to a path's end) belongs to it."""
PATH_JOIN_M: Final = 1.0
"""A path point this close to an area (or the mower) touches it."""

_HARDWARE_FAULT_WORDS: Final = ("lift", "tilt", "rollover", "blade", "motor", "overcurrent")
"""Words in an error code or message that mean a fault a dock command must not drive over."""

_NO_DOCK_ACTIVITIES: Final = {
    MowerActivity.DOCKED: "the mower is already docked",
    MowerActivity.CHARGING: "the mower is already docked",
    MowerActivity.RETURNING: "the mower is already returning to the dock",
    MowerActivity.OFFLINE: "the mower is offline",
    MowerActivity.UPDATING: "a firmware update runs",
}


class DockRoute(BaseModel):
    """Whether the generic return to dock is applicable, as the model sees it."""

    mower_area: str | None = Field(
        default=None, description="Name (or id) of the area the mower is in."
    )
    dock_area: str | None = Field(
        default=None, description="Name (or id) of the area the dock belongs to."
    )
    connected: bool = Field(
        default=False,
        description="The mower area and the dock are linked: the same area, or mapped "
        "transfer paths between them.",
    )
    route: list[str] = Field(
        default_factory=list, description="Areas from the mower's to the dock's, in order."
    )
    via_paths: int = Field(default=0, description="Transfer paths on that route.")
    dock_distance_m: float | None = Field(
        default=None, description="Straight-line distance mower to dock."
    )
    return_to_dock_applicable: bool = False
    reason: str = Field(description="Why return to dock is applicable or not.")


def polygon_distance(p: LocalPoint, polygon: Sequence[LocalPoint]) -> float:
    """0 inside the polygon, else the distance to its boundary."""
    return 0.0 if point_in_polygon(p, polygon) else boundary_distance(p, polygon)


def _label(area: MapArea) -> str:
    return area.name or area.id


def _touches(points: list[LocalPoint], area: MapArea) -> bool:
    return any(polygon_distance(p, area.boundary) <= PATH_JOIN_M for p in points)


def _path_near(points: list[LocalPoint], target: LocalPoint, radius: float) -> bool:
    """A point of the line (its segments, not only the vertices) within ``radius``."""
    if len(points) == 1:
        return distance(points[0], target) <= radius
    return any(segment_distance(target, a, b) <= radius for a, b in itertools.pairwise(points))


def hardware_fault(state: MowerState) -> str | None:
    """The fault a dock command must not drive over (lift, tilt, sensor stops), or None."""
    if state.raw.get("lifted") is True:
        return "the mower was lifted"
    if state.activity is not MowerActivity.ERROR:
        return None
    text = f"{state.error_code or ''} {state.error_message or ''}".lower()
    if any(word in text for word in _HARDWARE_FAULT_WORDS):
        return f"error {state.error_code or 'unknown'} is a lift, tilt, blade or motor fault"
    if not state.drive_recoverable:
        return f"error {state.error_code or 'unknown'} is a safety stop"
    return None


def state_problem(state: MowerState | None) -> str | None:
    """Why the mower state rules out a return to dock (None: it does not)."""
    if state is None:
        return "no state of the mower is known"
    fault = hardware_fault(state)
    if fault is not None:
        return fault
    return _NO_DOCK_ACTIVITIES.get(state.activity)


def dock_route(
    mower_map: MowerMap | None, here: LocalPoint | None, state: MowerState | None
) -> DockRoute:
    """The route from the mower to the dock over the map and whether return to dock fits."""
    if mower_map is None or mower_map.dock is None:
        return DockRoute(reason="no map with a dock is known")
    if here is None:
        return DockRoute(reason="the position of the mower is unknown")
    dock = (mower_map.dock.x, mower_map.dock.y)
    route = DockRoute(dock_distance_m=round(distance(here, dock), 1), reason="")
    areas = {area.id: area for area in mower_map.areas}
    links: dict[str, set[str]] = {area_id: set() for area_id in areas}
    dock_areas = {
        area.id
        for area in mower_map.areas
        if polygon_distance(dock, area.boundary) <= DOCK_AREA_MAX_M
    }
    path_areas: list[tuple[set[str], list[LocalPoint]]] = []
    for path in mower_map.paths:
        points = list(path.points)
        touched = {area.id for area in mower_map.areas if _touches(points, area)}
        path_areas.append((touched, points))
        for a in touched:
            links[a] |= touched - {a}
        if _path_near([points[0]], dock, DOCK_AREA_MAX_M) or _path_near(
            [points[-1]], dock, DOCK_AREA_MAX_M
        ):
            dock_areas |= touched
    start = next((a.id for a in mower_map.areas if point_in_polygon(here, a.boundary)), None)
    if start is None:
        near = [a for a in mower_map.areas if polygon_distance(here, a.boundary) <= PATH_JOIN_M]
        start = min(near, key=lambda a: polygon_distance(here, a.boundary)).id if near else None
    starts = {start} if start else set()
    if not starts:
        for touched, points in path_areas:
            if _path_near(points, here, PATH_JOIN_M):
                starts |= touched
    if start:
        route.mower_area = _label(areas[start])
    if dock_areas:
        nearest = min(dock_areas, key=lambda a: polygon_distance(dock, areas[a].boundary))
        route.dock_area = _label(areas[nearest])
    found = _shortest(starts, dock_areas, links)
    if found:
        route.connected = True
        route.route = [_label(areas[a]) for a in found]
        route.via_paths = len(found) - 1
        if not start:
            route.mower_area = route.mower_area or f"on a path near {route.route[0]}"
    problem = _problem(mower_map, here, state, route, bool(starts), bool(dock_areas))
    route.return_to_dock_applicable = problem is None
    route.reason = problem or (
        "the mower is in the dock's area"
        if route.via_paths == 0
        else f"mapped transfer paths link the mower's area to the dock ({route.via_paths})"
    )
    return route


def _problem(
    mower_map: MowerMap,
    here: LocalPoint,
    state: MowerState | None,
    route: DockRoute,
    in_area: bool,
    dock_known: bool,
) -> str | None:
    blocked = state_problem(state)
    if blocked is not None:
        return blocked
    for zone in mower_map.forbidden_zones():
        if point_in_polygon(here, zone.boundary):
            return f"the mower is inside the no-go zone or obstacle {zone.name or zone.id}"
    if not in_area:
        return "the mower is outside the mapped areas and paths"
    if not dock_known:
        return "no mapped area belongs to the dock"
    if not route.connected:
        return "no mapped path links the mower's area to the dock"
    return None


def _shortest(starts: set[str], goals: set[str], links: dict[str, set[str]]) -> list[str]:
    """Breadth first: the fewest areas from a start to a goal (empty when none)."""
    if not starts or not goals:
        return []
    queue: deque[list[str]] = deque([[s] for s in sorted(starts)])
    seen = set(starts)
    while queue:
        chain = queue.popleft()
        if chain[-1] in goals:
            return chain
        for nxt in sorted(links.get(chain[-1], ())):
            if nxt not in seen:
                seen.add(nxt)
                queue.append([*chain, nxt])
    return []
