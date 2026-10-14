"""Geometry helpers and the simulator's stripe path."""

import math
from itertools import pairwise

import pytest
from mowdeck_contracts.v1 import GeoReference, LocalPoint
from mowdeck_control import default_map
from mowdeck_control.geo import (
    compass_heading,
    distance,
    from_wgs84,
    point_in_polygon,
    rotate,
    scanline_intervals,
    segment_is_clear,
    to_geo_position,
    to_wgs84,
)
from mowdeck_control.paths import stripe_path

SQUARE: list[LocalPoint] = [(0, 0), (10, 0), (10, 10), (0, 10)]


@pytest.mark.parametrize("rotation", [0.0, 12.5, -90.0, 270.0])
@pytest.mark.parametrize("point", [(0.0, 0.0), (12.3, -4.5), (-150.0, 220.0)])
def test_wgs84_round_trip(rotation: float, point: tuple[float, float]) -> None:
    ref = GeoReference(origin_lat=48.137154, origin_lon=11.576124, rotation_deg=rotation)
    lat, lon = to_wgs84(ref, *point)
    x, y = from_wgs84(ref, lat, lon)
    assert x == pytest.approx(point[0], abs=1e-6)
    assert y == pytest.approx(point[1], abs=1e-6)


def test_wgs84_axes_and_map_argument() -> None:
    ref = GeoReference(origin_lat=48.0, origin_lon=11.0)
    lat_n, lon_n = to_wgs84(ref, 0, 111.32)  # about 0.001 degrees north
    assert lat_n == pytest.approx(48.001, abs=1e-5)
    assert lon_n == pytest.approx(11.0)
    lat_e, lon_e = to_wgs84(ref, 100, 0)
    assert lat_e == pytest.approx(48.0)
    assert lon_e > 11.0
    rotated = GeoReference(origin_lat=48.0, origin_lon=11.0, rotation_deg=90)
    lat_r, lon_r = to_wgs84(rotated, 100, 0)  # local +x points north
    assert lat_r > 48.0
    assert lon_r == pytest.approx(11.0)
    mower_map = default_map()
    assert to_wgs84(mower_map, 1, 2) == to_wgs84(mower_map.reference, 1, 2)


def test_compass_heading() -> None:
    ref = GeoReference(origin_lat=0, origin_lon=0)
    assert compass_heading(ref, 0.0) == pytest.approx(90.0)  # +x is east
    assert compass_heading(ref, math.pi / 2) == pytest.approx(0.0)  # +y is north
    assert compass_heading(ref, math.pi) == pytest.approx(270.0)
    rotated = GeoReference(origin_lat=0, origin_lon=0, rotation_deg=90)
    assert compass_heading(rotated, 0.0) == pytest.approx(0.0)
    position = to_geo_position(ref, 0, 0, -math.pi / 2)
    assert position.heading == pytest.approx(180.0)
    assert to_geo_position(ref, 0, 0).heading is None


def test_point_in_polygon_and_scanline() -> None:
    assert point_in_polygon((5, 5), SQUARE)
    assert not point_in_polygon((11, 5), SQUARE)
    assert not point_in_polygon((5, -0.1), SQUARE)
    l_shape: list[LocalPoint] = [(0, 0), (4, 0), (4, 1), (1, 1), (1, 4), (0, 4)]
    assert point_in_polygon((0.5, 3), l_shape)
    assert not point_in_polygon((3, 3), l_shape)
    assert scanline_intervals(SQUARE, 5) == [(0, 10)]
    assert scanline_intervals(SQUARE, 11) == []
    u_shape: list[LocalPoint] = [(0, 0), (6, 0), (6, 4), (4, 4), (4, 1), (2, 1), (2, 4), (0, 4)]
    assert scanline_intervals(u_shape, 2) == [(0, 2), (4, 6)]


def test_segment_is_clear_and_helpers() -> None:
    zone: list[LocalPoint] = [(4, 4), (6, 4), (6, 6), (4, 6)]
    assert segment_is_clear((1, 1), (9, 1), inside=SQUARE, outside=[zone])
    assert not segment_is_clear((1, 5), (9, 5), inside=SQUARE, outside=[zone])
    assert not segment_is_clear((1, 1), (11, 1), inside=SQUARE, outside=[])
    assert segment_is_clear((-5, -5), (-1, -1), inside=None, outside=[zone])
    assert segment_is_clear((1, 1), (1, 1), inside=SQUARE, outside=[])
    assert distance((0, 0), (3, 4)) == 5
    x, y = rotate((1, 0), math.pi / 2)
    assert x == pytest.approx(0.0, abs=1e-12)
    assert y == pytest.approx(1.0)


def _assert_path_safe(
    path: list[LocalPoint], area: list[LocalPoint], zones: list[list[LocalPoint]]
) -> None:
    assert path
    for a, b in pairwise(path):
        assert segment_is_clear(a, b, inside=area, outside=zones, step_m=0.02)


@pytest.mark.parametrize("angle", [0.0, 30.0, 45.0, 90.0, 135.0])
def test_stripe_path_stays_inside_and_out_of_zones(angle: float) -> None:
    mower_map = default_map()
    zones = [zone.boundary for zone in mower_map.forbidden_zones()]
    for area in mower_map.areas:
        path = stripe_path(area.boundary, zones, spacing_m=0.25, angle_deg=angle, margin_m=0.2)
        _assert_path_safe(path, area.boundary, zones)


def test_stripe_path_covers_a_square() -> None:
    path = stripe_path(SQUARE, spacing_m=1.0, margin_m=0.5)
    ys = sorted({round(p[1], 6) for p in path})
    assert ys == [0.5 + i for i in range(10)]
    assert path[0] == (0.5, 0.5)
    assert path[1] == (9.5, 0.5)


def test_stripe_path_skips_unreachable_stripes_around_a_central_zone() -> None:
    zone: list[LocalPoint] = [(4, 4), (6, 4), (6, 6), (4, 6)]
    path = stripe_path(SQUARE, [zone], spacing_m=0.5, margin_m=0.25)
    _assert_path_safe(path, SQUARE, [zone])


def test_stripe_path_degenerate_inputs() -> None:
    tiny: list[LocalPoint] = [(0, 0), (0.2, 0), (0.2, 0.2), (0, 0.2)]
    assert stripe_path(tiny, spacing_m=0.25, margin_m=0.2) == []
    with pytest.raises(ValueError, match="spacing_m"):
        stripe_path(SQUARE, spacing_m=0)
    with pytest.raises(ValueError, match="margin_m"):
        stripe_path(SQUARE, spacing_m=1, margin_m=-1)
