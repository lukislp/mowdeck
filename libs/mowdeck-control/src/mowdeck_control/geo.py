"""Geometry helpers: local metres to WGS84 and back, polygons.

The local frame is the one of :class:`~mowdeck_contracts.v1.MowerMap`: ``(x, y)`` in metres,
anchored by a :class:`~mowdeck_contracts.v1.GeoReference`. The conversion uses a local tangent
plane (equirectangular) approximation, which is accurate to millimetres over garden distances
of a few hundred metres.
"""

import math
from collections.abc import Sequence

from mowdeck_contracts.v1 import GeoPosition, GeoReference, LocalPoint, MowerMap

EARTH_RADIUS_M = 6_378_137.0
"""WGS84 equatorial radius."""


def _reference(ref: GeoReference | MowerMap) -> GeoReference:
    return ref.reference if isinstance(ref, MowerMap) else ref


def to_wgs84(ref: GeoReference | MowerMap, x: float, y: float) -> tuple[float, float]:
    """Convert a local point to ``(lat, lon)`` in degrees."""
    reference = _reference(ref)
    r = math.radians(reference.rotation_deg)
    east = x * math.cos(r) - y * math.sin(r)
    north = x * math.sin(r) + y * math.cos(r)
    lat = reference.origin_lat + math.degrees(north / EARTH_RADIUS_M)
    lon_scale = EARTH_RADIUS_M * math.cos(math.radians(reference.origin_lat))
    lon = reference.origin_lon + math.degrees(east / lon_scale)
    return lat, lon


def from_wgs84(ref: GeoReference | MowerMap, lat: float, lon: float) -> tuple[float, float]:
    """Convert ``(lat, lon)`` in degrees to a local point; the inverse of :func:`to_wgs84`."""
    reference = _reference(ref)
    north = math.radians(lat - reference.origin_lat) * EARTH_RADIUS_M
    lon_scale = EARTH_RADIUS_M * math.cos(math.radians(reference.origin_lat))
    east = math.radians(lon - reference.origin_lon) * lon_scale
    r = math.radians(reference.rotation_deg)
    x = east * math.cos(r) + north * math.sin(r)
    y = -east * math.sin(r) + north * math.cos(r)
    return x, y


def compass_heading(ref: GeoReference | MowerMap, local_angle_rad: float) -> float:
    """Convert a direction in the local frame (radians, counter-clockwise from +x) to a compass
    heading in degrees (0 = north, clockwise), in ``[0, 360)``."""
    enu_deg = math.degrees(local_angle_rad) + _reference(ref).rotation_deg
    heading = (90.0 - enu_deg) % 360.0
    return 0.0 if heading >= 360.0 else heading  # guard against -0.0 % 360 rounding to 360.0


def local_angle(ref: GeoReference | MowerMap, heading_deg: float) -> float:
    """Convert a compass heading (degrees, 0 = north, clockwise) to a direction in the local
    frame (radians, counter-clockwise from +x); the inverse of :func:`compass_heading`."""
    return math.radians(90.0 - heading_deg - _reference(ref).rotation_deg)


def to_geo_position(
    ref: GeoReference | MowerMap, x: float, y: float, local_angle_rad: float | None = None
) -> GeoPosition:
    """Build a :class:`GeoPosition` for a local point and optional local direction."""
    lat, lon = to_wgs84(ref, x, y)
    heading = None if local_angle_rad is None else compass_heading(ref, local_angle_rad)
    return GeoPosition(lat=lat, lon=lon, heading=heading)


def point_in_polygon(point: LocalPoint, polygon: Sequence[LocalPoint]) -> bool:
    """Even-odd ray casting test. Points exactly on an edge may count as inside or outside."""
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


def distance(a: LocalPoint, b: LocalPoint) -> float:
    """Euclidean distance in metres."""
    return math.hypot(b[0] - a[0], b[1] - a[1])


def segment_distance(p: LocalPoint, a: LocalPoint, b: LocalPoint) -> float:
    """Distance from ``p`` to the straight segment ``a``-``b`` in metres."""
    ax, ay = a
    bx, by = b
    dx, dy = bx - ax, by - ay
    length = dx * dx + dy * dy
    if length == 0:
        return distance(p, a)
    t = max(0.0, min(1.0, ((p[0] - ax) * dx + (p[1] - ay) * dy) / length))
    return distance(p, (ax + t * dx, ay + t * dy))


def boundary_distance(p: LocalPoint, polygon: Sequence[LocalPoint]) -> float:
    """Distance from ``p`` to the nearest edge of ``polygon`` (inside or outside)."""
    n = len(polygon)
    return min(segment_distance(p, polygon[i], polygon[(i + 1) % n]) for i in range(n))


def wrap_angle(angle_rad: float) -> float:
    """Normalise an angle to ``(-pi, pi]``."""
    return math.pi - (math.pi - angle_rad) % (2 * math.pi)


def rotate(point: LocalPoint, angle_rad: float) -> LocalPoint:
    """Rotate ``point`` counter-clockwise around the origin."""
    c, s = math.cos(angle_rad), math.sin(angle_rad)
    return (point[0] * c - point[1] * s, point[0] * s + point[1] * c)


def scanline_intervals(polygon: Sequence[LocalPoint], y: float) -> list[tuple[float, float]]:
    """Return the sorted ``(x_start, x_end)`` intervals where the horizontal line at ``y`` lies
    inside ``polygon`` (even-odd rule)."""
    crossings: list[float] = []
    n = len(polygon)
    for i in range(n):
        x1, y1 = polygon[i]
        x2, y2 = polygon[(i + 1) % n]
        if (y1 > y) != (y2 > y):
            crossings.append(x1 + (y - y1) * (x2 - x1) / (y2 - y1))
    crossings.sort()
    return [(crossings[i], crossings[i + 1]) for i in range(0, len(crossings) - 1, 2)]


def segment_is_clear(
    start: LocalPoint,
    end: LocalPoint,
    *,
    inside: Sequence[LocalPoint] | None,
    outside: Sequence[Sequence[LocalPoint]],
    step_m: float = 0.05,
) -> bool:
    """Sample the straight segment ``start``-``end`` every ``step_m`` metres and check that every
    sample is inside ``inside`` (if given) and outside every polygon in ``outside``."""
    length = distance(start, end)
    steps = max(1, math.ceil(length / step_m))
    for i in range(steps + 1):
        t = i / steps
        p = (start[0] + (end[0] - start[0]) * t, start[1] + (end[1] - start[1]) * t)
        if inside is not None and not point_in_polygon(p, inside):
            return False
        if any(point_in_polygon(p, zone) for zone in outside):
            return False
    return True
