"""Area, MowerSpec, Segment and MowPlan, including the contract form."""

import math

import pytest
from mowdeck_contracts.v1 import MapArea, MapZone, MowerMap, SpotNativeJob
from mowdeck_contracts.v1.maps import GeoReference
from mowdeck_patterns import (
    Area,
    InvalidArea,
    MowerSpec,
    MowPlan,
    PatternError,
    Segment,
    default_registry,
)
from mowdeck_patterns.models import polygonal, polygons_of
from patterns_support import FLOWERBED, FRONT_BOUNDARY, MOWER, front_area
from shapely.geometry import GeometryCollection, LineString, Point, Polygon


def _map() -> MowerMap:
    return MowerMap(
        reference=GeoReference(origin_lat=48.0, origin_lon=11.0),
        areas=[MapArea(id="front", boundary=FRONT_BOUNDARY)],
        no_go_zones=[
            MapZone(id="bed", boundary=FLOWERBED),
            MapZone(id="far", boundary=[(50, 50), (51, 50), (51, 51)]),
        ],
        obstacles=[MapZone(id="tree", boundary=[(2, 2), (2.5, 2), (2.5, 2.5)])],
    )


def test_area_of_drops_the_closing_point_and_computes_the_region() -> None:
    area = Area.of([*FRONT_BOUNDARY, FRONT_BOUNDARY[0]], no_go_zones=[FLOWERBED])
    assert len(area.boundary) == 4
    assert area.region().area == pytest.approx(96 - 7.5)
    assert area.outline().area == pytest.approx(96)
    assert area.forbidden().area == pytest.approx(7.5)


@pytest.mark.parametrize(
    ("boundary", "message"),
    [
        ([(0, 0), (1, 1)], "at least three points"),
        ([(0, 0), (1, 0), (math.nan, 1)], "not finite"),
        ([(0, 0), (2000, 0), (2000, 1)], "wider than"),
        ([(0, 0), (500, 0), (500, 500), (0, 500)], "at most"),
        ([(0, 0), (0.01, 0), (0.01, 0.01)], "empty"),
    ],
)
def test_area_rejects_unusable_shapes(boundary: list[tuple[float, float]], message: str) -> None:
    with pytest.raises(InvalidArea, match=message):
        Area.of(boundary)


def test_area_rejects_too_many_vertices() -> None:
    ring = [(math.cos(i / 1000), math.sin(i / 1000)) for i in range(5001)]
    with pytest.raises(InvalidArea, match="vertices"):
        Area.of(ring)


def test_area_from_map_takes_touching_zones_only() -> None:
    area = Area.from_map(_map(), "front")
    assert area.no_go_zones == (tuple(FLOWERBED),)
    assert len(area.obstacles) == 1
    spot = Area.from_map(_map(), "front", boundary=[(1, 1), (5, 1), (5, 4), (1, 4)], holes=[])
    assert spot.region().area < 12
    with pytest.raises(InvalidArea, match="unknown area"):
        Area.from_map(_map(), "back")


def test_mower_spec_checks_its_values() -> None:
    assert MOWER.spacing_m == pytest.approx(0.35)
    assert MowerSpec(cutting_width_m=0.2, overlap_m=0.0, safety_margin_m=0.3).inset_m == 0.3
    with pytest.raises(PatternError, match="overlap_m"):
        MowerSpec(cutting_width_m=0.4, overlap_m=0.4)
    with pytest.raises(PatternError, match="max_speed_mps"):
        MowerSpec(max_speed_mps=0)


def test_segment_checks_its_values() -> None:
    with pytest.raises(PatternError):
        Segment(((0.0, 0.0),), True, 0.4)
    with pytest.raises(PatternError):
        Segment(((0.0, 0.0), (1.0, 0.0)), True, 0.0)
    segment = Segment(((0.0, 0.0), (3.0, 4.0), (3.0, 5.0)), True, 0.5)
    assert segment.length == pytest.approx(6.0)
    assert segment.start == (0.0, 0.0)
    assert segment.end == (3.0, 5.0)


def test_plan_round_trips_through_the_contract() -> None:
    area = front_area()
    plan = default_registry().get("border_fill").compute(area, MOWER, {})
    spot = plan.to_spot_plan(SpotNativeJob(area_ids=["front"], pattern={"name": "stripes"}))
    assert spot.pattern_chain == ["border_fill"]
    assert spot.native is not None
    assert len(spot.segments) == len(plan.segments)
    again = MowPlan.from_spot_plan(spot)
    assert [s.kind for s in again.segments] == [s.kind for s in plan.segments]
    assert again.mow_length_m == pytest.approx(plan.mow_length_m, abs=0.5)
    assert len(spot.model_dump_json()) < 20_000
    assert spot.point_count >= len(spot.segments) * 2


def test_plan_without_area_has_no_ratio() -> None:
    plan = MowPlan(
        segments=(Segment(((0.0, 0.0), (1.0, 0.0)), False, 0.5, "transfer"),),
        estimated_duration_s=2,
        covered_area_m2=0,
        pattern_chain=("x",),
    )
    assert plan.coverage_ratio == 0.0
    assert plan.transfer_length_m == pytest.approx(1.0)


def test_degenerate_segments_are_dropped_in_the_contract() -> None:
    plan = MowPlan(
        segments=(
            Segment(((1.0, 1.0), (1.001, 1.001)), True, 0.4),
            Segment(((1.0, 1.0), (2.0, 1.0)), True, 0.4),
        ),
        estimated_duration_s=2,
        covered_area_m2=0,
        pattern_chain=("x",),
    )
    assert len(plan.to_spot_plan().segments) == 1


def test_polygon_helpers() -> None:
    collection = GeometryCollection(
        [Polygon([(0, 0), (1, 0), (1, 1)]), LineString([(0, 0), (1, 1)])]
    )
    assert len(polygons_of(collection)) == 1
    assert polygonal(Point(0, 0)).is_empty
    two = GeometryCollection([Polygon([(0, 0), (1, 0), (1, 1)]), Polygon([(5, 5), (6, 5), (6, 6)])])
    assert polygonal(two).geom_type == "MultiPolygon"
