"""Aerial photo tiles: the pure geometry (Web Mercator tiles, the map's bounding box, the
upstream request).

The mower map lives in local metres anchored by a ``GeoReference``; the browser draws it in
EPSG:3857 (Leaflet). A tile ``z/x/y`` is therefore requested from the photo source for exactly
its EPSG:3857 box, so the image lines up with the map without any client side reprojection.
"""

import math
from dataclasses import dataclass
from urllib.parse import urlencode

from mowdeck_contracts.v1 import MowerMap
from mowdeck_control.geo import to_wgs84

WORLD_HALF_M = 20_037_508.342789244
"""Half the width of the Web Mercator world in metres."""

MAX_LAT = 85.0511287798066
"""Latitude limit of Web Mercator."""


@dataclass(frozen=True, slots=True)
class Bounds:
    """A box in WGS84 degrees."""

    south: float
    west: float
    north: float
    east: float

    def intersects(self, other: "Bounds") -> bool:
        """Whether the two boxes overlap (touching edges do not count)."""
        return (
            self.west < other.east
            and other.west < self.east
            and self.south < other.north
            and other.south < self.north
        )


def tile_in_range(z: int, x: int, y: int) -> bool:
    """Whether ``z/x/y`` names a tile of the XYZ scheme."""
    return 0 <= z <= 24 and 0 <= x < (1 << z) and 0 <= y < (1 << z)


def tile_bounds_3857(z: int, x: int, y: int) -> tuple[float, float, float, float]:
    """``(min_x, min_y, max_x, max_y)`` in EPSG:3857 metres of tile ``z/x/y`` (y grows south)."""
    size = 2 * WORLD_HALF_M / (1 << z)
    min_x = -WORLD_HALF_M + x * size
    max_y = WORLD_HALF_M - y * size
    return min_x, max_y - size, min_x + size, max_y


def mercator_to_wgs84(x: float, y: float) -> tuple[float, float]:
    """``(lat, lon)`` of an EPSG:3857 point."""
    lon = x / WORLD_HALF_M * 180.0
    lat = math.degrees(2 * math.atan(math.exp(y / WORLD_HALF_M * math.pi)) - math.pi / 2)
    return lat, lon


def wgs84_to_mercator(lat: float, lon: float) -> tuple[float, float]:
    """EPSG:3857 ``(x, y)`` of a WGS84 point."""
    lat = max(-MAX_LAT, min(MAX_LAT, lat))
    x = lon / 180.0 * WORLD_HALF_M
    y = math.log(math.tan(math.pi / 4 + math.radians(lat) / 2)) / math.pi * WORLD_HALF_M
    return x, y


def tile_bounds(z: int, x: int, y: int) -> Bounds:
    """The WGS84 box of tile ``z/x/y``."""
    min_x, min_y, max_x, max_y = tile_bounds_3857(z, x, y)
    south, west = mercator_to_wgs84(min_x, min_y)
    north, east = mercator_to_wgs84(max_x, max_y)
    return Bounds(south, west, north, east)


def map_bounds(mower_map: MowerMap, margin_m: float) -> Bounds:
    """The WGS84 box around everything on the map (areas, no-go zones, obstacles, paths, dock)
    and ``margin_m`` metres more on every side. An empty map is the reference point plus the
    margin. The rotation of the local frame is applied point by point, so the box is that of the
    rotated outline."""
    points: list[tuple[float, float]] = []
    for area in mower_map.areas:
        points.extend(area.boundary)
    for zone in (*mower_map.no_go_zones, *mower_map.obstacles):
        points.extend(zone.boundary)
    for path in mower_map.paths:
        points.extend(path.points)
    if mower_map.dock is not None:
        points.append((mower_map.dock.x, mower_map.dock.y))
    if not points:
        points = [(0.0, 0.0)]
    geo = [to_wgs84(mower_map.reference, px, py) for px, py in points]
    lats = [p[0] for p in geo]
    lons = [p[1] for p in geo]
    mid_lat = (min(lats) + max(lats)) / 2
    d_lat = math.degrees(margin_m / 6_378_137.0)
    d_lon = math.degrees(margin_m / (6_378_137.0 * max(math.cos(math.radians(mid_lat)), 0.01)))
    return Bounds(min(lats) - d_lat, min(lons) - d_lon, max(lats) + d_lat, max(lons) + d_lon)


def points_bounds(points: list[tuple[float, float]], margin_m: float) -> Bounds | None:
    """The WGS84 box around ``(lat, lon)`` points and ``margin_m`` metres more on every side;
    None without points. The base map of theft tracking serves tiles only inside it (ADR 0058)."""
    if not points:
        return None
    lats = [p[0] for p in points]
    lons = [p[1] for p in points]
    mid_lat = (min(lats) + max(lats)) / 2
    d_lat = math.degrees(margin_m / 6_378_137.0)
    d_lon = math.degrees(margin_m / (6_378_137.0 * max(math.cos(math.radians(mid_lat)), 0.01)))
    return Bounds(
        max(-90.0, min(lats) - d_lat),
        max(-180.0, min(lons) - d_lon),
        min(90.0, max(lats) + d_lat),
        min(180.0, max(lons) + d_lon),
    )


def xyz_url(template: str, z: int, x: int, y: int) -> str:
    """Fill ``{z}``, ``{x}`` and ``{y}`` of a tile URL template."""
    return template.replace("{z}", str(z)).replace("{x}", str(x)).replace("{y}", str(y))


def wms_get_map_url(
    base: str,
    *,
    layer: str,
    image_format: str,
    version: str,
    z: int,
    x: int,
    y: int,
    size: int,
) -> str:
    """The WMS ``GetMap`` URL for tile ``z/x/y`` in EPSG:3857 at ``size`` x ``size`` pixels.

    EPSG:3857 is east/north in both WMS versions (only geographic CRSs flip the axes of 1.3.0),
    so ``BBOX`` is ``min_x,min_y,max_x,max_y`` either way."""
    min_x, min_y, max_x, max_y = tile_bounds_3857(z, x, y)
    crs_key = "CRS" if version >= "1.3" else "SRS"
    query = {
        "SERVICE": "WMS",
        "VERSION": version,
        "REQUEST": "GetMap",
        "LAYERS": layer,
        "STYLES": "",
        crs_key: "EPSG:3857",
        "BBOX": f"{min_x:.4f},{min_y:.4f},{max_x:.4f},{max_y:.4f}",
        "WIDTH": str(size),
        "HEIGHT": str(size),
        "FORMAT": image_format,
    }
    separator = "&" if "?" in base else "?"
    return f"{base}{separator}{urlencode(query, safe=':,/')}"
