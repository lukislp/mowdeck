"""The geofence of a mower: its mapped areas, transfer paths and dock, plus a margin.

The fence is built from the map snapshot the Device-Gateway publishes (local metres plus a
:class:`~mowdeck_contracts.v1.GeoReference`). A WGS84 position is converted into the local frame
with the same equirectangular approximation ``mowdeck_control.geo`` uses (millimetre accurate
over garden distances); the formula is copied here so this service does not depend on the
control library.

The allowed region is the union of

* every area polygon, buffered by the margin,
* every transfer path (as a polyline), buffered by the margin,
* the dock, buffered by the margin.

:func:`distance_outside` answers how far a position lies outside that region (0 inside). No
geometry library is needed: a point-in-polygon test plus point-to-segment distances.
"""

import math
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

from mowdeck_contracts.v1 import MowerMap

EARTH_RADIUS_M = 6_378_137.0
"""WGS84 equatorial radius (as in ``mowdeck_control.geo``)."""

Point = tuple[float, float]


@dataclass(frozen=True, slots=True)
class Fence:
    """The geometry of a fence in the map's local frame (metres)."""

    origin_lat: float
    origin_lon: float
    rotation_deg: float
    areas: tuple[tuple[Point, ...], ...]
    paths: tuple[tuple[Point, ...], ...] = ()
    dock: Point | None = None

    @classmethod
    def from_map(cls, mower_map: MowerMap) -> "Fence | None":
        """The fence of a map; None for a map without areas or without a real geo reference
        (origin ``(0, 0)`` is the placeholder of maps that have none)."""
        reference = mower_map.reference
        if not mower_map.areas:
            return None
        if reference.origin_lat == 0 and reference.origin_lon == 0:
            return None
        dock = mower_map.dock
        return cls(
            origin_lat=reference.origin_lat,
            origin_lon=reference.origin_lon,
            rotation_deg=reference.rotation_deg,
            areas=tuple(tuple(area.boundary) for area in mower_map.areas),
            paths=tuple(tuple(path.points) for path in mower_map.paths),
            dock=(dock.x, dock.y) if dock is not None else None,
        )

    def to_json(self) -> dict[str, Any]:
        """JSON form for the store."""
        return {
            "reference": {
                "origin_lat": self.origin_lat,
                "origin_lon": self.origin_lon,
                "rotation_deg": self.rotation_deg,
            },
            "areas": [[list(p) for p in ring] for ring in self.areas],
            "paths": [[list(p) for p in line] for line in self.paths],
            "dock": list(self.dock) if self.dock is not None else None,
        }

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> "Fence":
        """Inverse of :meth:`to_json`."""
        reference = data["reference"]
        dock = data.get("dock")
        return cls(
            origin_lat=float(reference["origin_lat"]),
            origin_lon=float(reference["origin_lon"]),
            rotation_deg=float(reference.get("rotation_deg", 0.0)),
            areas=tuple(tuple((float(x), float(y)) for x, y in ring) for ring in data["areas"]),
            paths=tuple(
                tuple((float(x), float(y)) for x, y in line) for line in data.get("paths", [])
            ),
            dock=(float(dock[0]), float(dock[1])) if dock else None,
        )

    def to_local(self, lat: float, lon: float) -> Point:
        """A WGS84 position in the local frame (``mowdeck_control.geo.from_wgs84``)."""
        north = math.radians(lat - self.origin_lat) * EARTH_RADIUS_M
        lon_scale = EARTH_RADIUS_M * math.cos(math.radians(self.origin_lat))
        east = math.radians(lon - self.origin_lon) * lon_scale
        r = math.radians(self.rotation_deg)
        return east * math.cos(r) + north * math.sin(r), -east * math.sin(r) + north * math.cos(r)


@dataclass(frozen=True, slots=True)
class MowerFence:
    """The stored fence of one mower."""

    mower_id: UUID
    account_id: UUID
    observed_at: datetime
    """``observed_at`` of the map snapshot it was built from (older snapshots are ignored)."""
    fence: Fence


def point_in_polygon(point: Point, polygon: Sequence[Point]) -> bool:
    """Even-odd ray casting test (a point on an edge may count either way)."""
    x, y = point
    inside = False
    n = len(polygon)
    for i in range(n):
        x1, y1 = polygon[i]
        x2, y2 = polygon[(i + 1) % n]
        if (y1 > y) != (y2 > y):
            x_cross = x1 + (y - y1) * (x2 - x1) / (y2 - y1)
            if x < x_cross:
                inside = not inside
    return inside


def segment_distance(point: Point, a: Point, b: Point) -> float:
    """Distance in metres from ``point`` to the segment ``a``-``b``."""
    px, py = point
    ax, ay = a
    dx, dy = b[0] - ax, b[1] - ay
    length2 = dx * dx + dy * dy
    t = 0.0 if length2 == 0 else max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / length2))
    return math.hypot(px - (ax + t * dx), py - (ay + t * dy))


def _ring_distance(point: Point, ring: Sequence[Point]) -> float:
    n = len(ring)
    return min(segment_distance(point, ring[i], ring[(i + 1) % n]) for i in range(n))


def _line_distance(point: Point, line: Sequence[Point]) -> float:
    if len(line) == 1:
        return math.hypot(point[0] - line[0][0], point[1] - line[0][1])
    return min(segment_distance(point, line[i], line[i + 1]) for i in range(len(line) - 1))


def fence_distance(fence: Fence, point: Point) -> float:
    """Distance in metres from a local point to the unbuffered fence geometry (0 inside an
    area)."""
    if any(point_in_polygon(point, ring) for ring in fence.areas):
        return 0.0
    distances = [_ring_distance(point, ring) for ring in fence.areas if ring]
    distances.extend(_line_distance(point, line) for line in fence.paths if line)
    if fence.dock is not None:
        distances.append(math.hypot(point[0] - fence.dock[0], point[1] - fence.dock[1]))
    return min(distances) if distances else math.inf


def distance_outside(fence: Fence, lat: float, lon: float, margin_m: float) -> float:
    """How many metres the position lies outside the allowed region (0 when inside it)."""
    return max(0.0, fence_distance(fence, fence.to_local(lat, lon)) - margin_m)
