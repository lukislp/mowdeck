"""A GeoJSON-like preview of a plan for the app: one line feature per segment plus totals.

Coordinates are local metres ``[x, y]`` (``crs: "local"``). With ``to_wgs84`` (a function
``(x, y) -> (lat, lon)``, for example ``mowdeck_control.geo.to_wgs84`` bound to the map's
reference) they become GeoJSON ``[lon, lat]`` and ``crs`` is ``"wgs84"``.
"""

from collections.abc import Callable
from typing import Any

from mowdeck_patterns.models import MowPlan, Point

LOCAL_DECIMALS = 3
WGS84_DECIMALS = 7


def preview(
    plan: MowPlan, to_wgs84: Callable[[float, float], tuple[float, float]] | None = None
) -> dict[str, Any]:
    """The plan as a ``FeatureCollection`` with summary ``properties``."""

    def convert(point: Point) -> list[float]:
        if to_wgs84 is None:
            return [round(point[0], LOCAL_DECIMALS), round(point[1], LOCAL_DECIMALS)]
        lat, lon = to_wgs84(point[0], point[1])
        return [round(lon, WGS84_DECIMALS), round(lat, WGS84_DECIMALS)]

    features = [
        {
            "type": "Feature",
            "geometry": {
                "type": "LineString",
                "coordinates": [convert(p) for p in segment.polyline],
            },
            "properties": {
                "index": index,
                "kind": segment.kind,
                "blades_on": segment.blades_on,
                "speed_mps": round(segment.speed, 3),
                "length_m": round(segment.length, 3),
            },
        }
        for index, segment in enumerate(plan.segments)
    ]
    return {
        "type": "FeatureCollection",
        "properties": {
            "crs": "local" if to_wgs84 is None else "wgs84",
            "pattern_chain": list(plan.pattern_chain),
            "estimated_duration_s": round(plan.estimated_duration_s, 1),
            "covered_area_m2": round(plan.covered_area_m2, 2),
            "area_m2": round(plan.area_m2, 2),
            "coverage_ratio": round(plan.coverage_ratio, 4),
            "mow_length_m": round(plan.mow_length_m, 2),
            "transfer_length_m": round(plan.transfer_length_m, 2),
            "segment_count": len(plan.segments),
            "warnings": list(plan.warnings),
        },
        "features": features,
    }
