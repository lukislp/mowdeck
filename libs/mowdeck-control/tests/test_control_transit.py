"""The transit planner (ADR 0041): routes on the neutral map through transfer paths and areas,
around no-go zones, from the dock; no route when the map does not connect start and goal."""

import itertools
import math
import time

import pytest
from mowdeck_contracts.v1 import (
    DockPose,
    GeoReference,
    LocalPoint,
    MapArea,
    MapPath,
    MapZone,
    MowerMap,
)
from mowdeck_control import DrivableSpace, NoRouteError, TransitConfig, default_map, plan_transit
from mowdeck_control.transit import densify, segment_segment_distance, simplify

REF = GeoReference(origin_lat=48.1, origin_lon=11.5)


def rect(x0: float, y0: float, x1: float, y1: float) -> list[LocalPoint]:
    return [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]


def assert_drivable(mower_map: MowerMap, points: tuple[LocalPoint, ...]) -> None:
    """Every 5 cm of the route lies in the drivable space."""
    space = DrivableSpace.of(mower_map)
    for a, b in itertools.pairwise(points):
        assert space.segment_clear_sampled(a, b), f"{a} -> {b} leaves the drivable space"


def test_control_transit_route_through_a_path_between_two_areas() -> None:
    garden = default_map()  # front (0..12 x 0..8), back (0..10 x 11..17), side path x = 1
    route = plan_transit(garden, (6.0, 4.0), (5.0, 14.0))
    assert route.points[0] == (6.0, 4.0) and route.points[-1] == (5.0, 14.0)
    assert any(abs(p[0] - 1.0) < 1e-6 and 7.0 <= p[1] <= 12.0 for p in route.points), route
    assert 10.0 < route.length_m < 16.0
    assert_drivable(garden, route.points)


def test_control_transit_starts_at_the_dock() -> None:
    garden = default_map()  # the dock (-1, 1) lies left of the front lawn
    route = plan_transit(garden, (-1.0, 1.0), (6.0, 4.0))
    assert route.points[0] == (-1.0, 1.0)
    assert route.length_m == pytest.approx(math.dist((-1.0, 1.0), (6.0, 4.0)), abs=0.6)
    assert_drivable(garden, route.points)
    after_undock = plan_transit(garden, (0.0, 1.0), (5.0, 14.0))
    assert_drivable(garden, after_undock.points)


def test_control_transit_no_route_when_a_no_go_zone_blocks_the_path() -> None:
    garden = default_map()
    blocked = garden.model_copy(
        update={"no_go_zones": [MapZone(id="shed", boundary=rect(0.0, 9.0, 2.0, 10.0))]}
    )
    with pytest.raises(NoRouteError, match="no drivable route"):
        plan_transit(blocked, (6.0, 4.0), (5.0, 14.0))
    # the same zone beside the path leaves it open
    beside = garden.model_copy(
        update={"no_go_zones": [MapZone(id="shed", boundary=rect(2.0, 9.0, 3.0, 10.0))]}
    )
    assert plan_transit(beside, (6.0, 4.0), (5.0, 14.0)).length_m > 0


def test_control_transit_across_two_paths_and_three_areas() -> None:
    # a - path - b - path - c, the paths meet b on different sides
    garden = MowerMap(
        reference=REF,
        areas=[
            MapArea(id="a", boundary=rect(0, 0, 10, 10)),
            MapArea(id="b", boundary=rect(20, 0, 30, 10)),
            MapArea(id="c", boundary=rect(20, 30, 30, 40)),
        ],
        paths=[
            MapPath(id="ab", points=[(10, 5), (15, 5), (20, 5)]),
            MapPath(id="bc", points=[(25, 10), (25, 20), (25, 30)]),
        ],
    )
    route = plan_transit(garden, (2.0, 2.0), (25.0, 35.0))
    assert_drivable(garden, route.points)
    space = DrivableSpace.of(garden)
    assert any(space.area_index(p) == 1 for p in route.points), "the route does not cross b"
    assert 40.0 < route.length_m < 52.0


def test_control_transit_passes_an_intermediate_l_shaped_area() -> None:
    # b is an L: its foot (20..40 x 0..10) and its leg (20..30 x 10..40). The path from a ends
    # at the foot's far right end, the path to c leaves the leg's top: the straight line
    # between both path ends leaves b, so the route turns at b's inner corner.
    garden = MowerMap(
        reference=REF,
        areas=[
            MapArea(id="a", boundary=rect(50, 0, 60, 10)),
            MapArea(
                id="b",
                boundary=[(20, 0), (40, 0), (40, 10), (30, 10), (30, 40), (20, 40)],
            ),
            MapArea(id="c", boundary=rect(20, 50, 30, 60)),
        ],
        paths=[
            MapPath(id="ab", points=[(50, 5), (40, 5)]),
            MapPath(id="bc", points=[(25, 40), (25, 50)]),
        ],
    )
    route = plan_transit(garden, (55.0, 5.0), (25.0, 55.0))
    assert_drivable(garden, route.points)
    corner = (30.0, 10.0)
    assert min(math.dist(p, corner) for p in route.points) < 1.5, route.points
    straight_through_b = math.dist((40, 5), (25, 40))
    assert route.length_m > 10 + straight_through_b + 10


def test_control_transit_far_garden_without_connection_is_no_route() -> None:
    garden = MowerMap(
        reference=REF,
        areas=[
            MapArea(id="home", boundary=rect(0, 0, 10, 10)),
            MapArea(id="far", boundary=rect(200, 0, 210, 10)),
        ],
        dock=DockPose(x=5, y=5),
    )
    with pytest.raises(NoRouteError, match="no drivable route"):
        plan_transit(garden, (5.0, 5.0), (205.0, 5.0))


def test_control_transit_honours_the_maximum_length() -> None:
    garden = MowerMap(
        reference=REF,
        areas=[
            MapArea(id="a", boundary=rect(0, 0, 10, 10)),
            MapArea(id="b", boundary=rect(100, 0, 110, 10)),
        ],
        paths=[MapPath(id="long", points=[(10, 5), (100, 5)])],
    )
    assert plan_transit(garden, (5, 5), (105, 5)).length_m == pytest.approx(100, abs=1)
    with pytest.raises(NoRouteError, match="longer than the limit of 50 m"):
        plan_transit(garden, (5, 5), (105, 5), TransitConfig(max_length_m=50))


def test_control_transit_goes_around_an_obstacle_inside_an_area() -> None:
    garden = MowerMap(
        reference=REF,
        areas=[MapArea(id="lawn", boundary=rect(0, 0, 20, 10))],
        obstacles=[MapZone(id="tree", boundary=rect(9, 2, 11, 8))],
    )
    route = plan_transit(garden, (2.0, 5.0), (18.0, 5.0))
    assert_drivable(garden, route.points)
    assert route.length_m > 16.5 and len(route.points) > 2


def test_control_transit_rejects_starts_and_goals_outside() -> None:
    garden = default_map()
    with pytest.raises(NoRouteError, match="the mower stands"):
        plan_transit(garden, (30.0, 30.0), (6.0, 4.0))
    with pytest.raises(NoRouteError, match="start of the spot"):
        plan_transit(garden, (6.0, 4.0), (10.5, 6.5))  # in the flower bed's margin
    same = plan_transit(garden, (6.0, 4.0), (6.0, 4.0))
    assert same.is_trivial


def test_control_transit_is_fast_on_a_detailed_map() -> None:
    def ring(x0: float, x1: float) -> list[LocalPoint]:
        out: list[LocalPoint] = []
        for (ax, ay), (bx, by) in itertools.pairwise([*rect(x0, 0, x1, 30), (x0, 0)]):
            out.extend((ax + (bx - ax) * i / 100, ay + (by - ay) * i / 100) for i in range(100))
        return out

    garden = MowerMap(
        reference=REF,
        areas=[MapArea(id=f"a{i}", boundary=ring(i * 60, i * 60 + 40)) for i in range(3)],
        paths=[
            MapPath(
                id=f"p{i}",
                points=[(i * 60 + 39.9 + t * 0.101, 15 + math.sin(t / 20)) for t in range(201)],
            )
            for i in range(2)
        ],
        obstacles=[
            MapZone(id=f"o{i}", boundary=rect(x, y, x + 2, y + 2))
            for i, (x, y) in enumerate([(10, 10), (20, 20), (70, 14), (130, 5)])
        ],
    )
    began = time.perf_counter()
    route = plan_transit(garden, (2.0, 2.0), (150.0, 25.0))
    assert time.perf_counter() - began < 5.0
    assert_drivable(garden, route.points)


def test_control_transit_geometry_helpers() -> None:
    assert segment_segment_distance((0, 0), (2, 2), (0, 2), (2, 0)) == 0.0
    assert segment_segment_distance((0, 0), (1, 0), (0, 1), (1, 1)) == pytest.approx(1.0)
    assert simplify([(0, 0), (1, 0.01), (2, 0)], 0.05) == [(0, 0), (2, 0)]
    assert len(densify([(0, 0), (3, 0)], 1.0)) == 4
    with pytest.raises(ValueError, match="positive"):
        TransitConfig(path_half_width_m=0)
