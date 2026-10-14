"""Property checks of the built-in patterns on sample shapes."""

from collections.abc import Callable
from typing import Any

import pytest
from mowdeck_contracts.v1 import MowerCapabilities, StartMow
from mowdeck_control.translation import MowRequest, plan_to_native_job
from mowdeck_patterns import (
    Area,
    InvalidSettings,
    MowerSpec,
    PlanningError,
    check,
    default_registry,
)
from mowdeck_patterns.patterns import BUILTIN_PATTERNS
from mowdeck_patterns.patterns.cells import decompose
from mowdeck_patterns.patterns.stripes import (
    AlternatingSettings,
    alternating_angle,
    best_angle,
)
from patterns_support import MOWER, SHAPES, front_area, holed
from shapely.geometry import LineString, Polygon

FILL_THRESHOLDS = {
    "stripes": 0.95,
    "diagonal": 0.95,
    "checkerboard": 0.97,
    "spiral": 0.95,
    "border_fill": 0.96,
    "contour": 0.95,
    "auto_aligned": 0.95,
    "cells": 0.95,
    "alternating": 0.95,
}
"""Least covered share of the mowable area per area-filling pattern."""


@pytest.mark.parametrize("shape", sorted(SHAPES))
@pytest.mark.parametrize("key", sorted(FILL_THRESHOLDS))
def test_fill_patterns_cover_the_area_and_pass_the_chain(key: str, shape: str) -> None:
    area = SHAPES[shape]()
    pattern = default_registry().get(key)
    plan = pattern.compute(area, MOWER, {})
    assert check(plan, area, MOWER) == []
    assert plan.coverage_ratio >= FILL_THRESHOLDS[key], (key, shape, plan.coverage_ratio)
    assert plan.pattern_chain == (key,)
    assert plan.estimated_duration_s > plan.mow_length_m / MOWER.max_speed_mps * 0.99


@pytest.mark.parametrize("key", sorted(BUILTIN_PATTERNS))
def test_no_blades_on_segment_enters_a_no_go_zone(key: str) -> None:
    area = front_area()
    settings: dict[str, Any] = (
        {"gaps": [[(8, 4), (11.5, 4), (11.5, 7.5), (8, 7.5)]]} if key == "remow" else {}
    )
    plan = default_registry().get(key).compute(area, MOWER, settings)
    zone = Polygon(area.no_go_zones[0])
    for segment in plan.segments:
        if segment.blades_on:
            assert LineString(segment.polyline).intersection(zone).length < 1e-6
        if segment.kind == "transfer":
            assert not segment.blades_on


@pytest.mark.parametrize("key", sorted(BUILTIN_PATTERNS))
def test_patterns_are_deterministic(key: str) -> None:
    settings: dict[str, Any] = (
        {"covered": [[(0, 0), (6, 0), (6, 8), (0, 8)]]} if key == "remow" else {}
    )
    pattern = BUILTIN_PATTERNS[key]()
    first = pattern.compute(holed(), MOWER, settings)
    second = pattern.compute(holed(), MOWER, settings)
    assert first == second


def test_edge_only_follows_the_edges_only() -> None:
    area = holed()
    plan = default_registry().get("edge_only").compute(area, MOWER, {"laps": 2})
    assert check(plan, area, MOWER) == []
    assert 0.15 < plan.coverage_ratio < 0.5
    mow = [s for s in plan.segments if s.kind == "mow"]
    assert len(mow) == 4  # two laps outside, two around the bed


def test_stripes_follow_the_angle() -> None:
    area = Area.of([(0, 0), (20, 0), (20, 10), (0, 10)])
    plan = default_registry().get("stripes").compute(area, MOWER, {"angle_deg": 90})
    runs = [s for s in plan.segments if s.kind == "mow"]
    for run in runs:
        (x0, _), (x1, _) = run.start, run.end
        assert abs(x0 - x1) < 1e-6


def test_diagonal_and_checkerboard_use_their_angles() -> None:
    area = Area.of([(0, 0), (10, 0), (10, 10), (0, 10)])
    diagonal = default_registry().get("diagonal").compute(area, MOWER, {})
    run = next(s for s in diagonal.segments if s.kind == "mow")
    dx, dy = run.end[0] - run.start[0], run.end[1] - run.start[1]
    assert abs(abs(dx) - abs(dy)) < 1e-6
    checker = default_registry().get("checkerboard").compute(area, MOWER, {"angle_deg": 0})
    single = default_registry().get("stripes").compute(area, MOWER, {"angle_deg": 0})
    assert checker.mow_length_m == pytest.approx(2 * single.mow_length_m, rel=0.02)


def test_auto_aligned_picks_the_long_side_of_a_narrow_strip() -> None:
    strip = Area.of([(0, 0), (3, 3), (2, 4), (-1, 1)])  # 4.2 x 1.4 m at 45 degrees
    work = strip.region()
    assert best_angle(work, MOWER.spacing_m) == pytest.approx(45.0)
    auto = default_registry().get("auto_aligned").compute(strip, MOWER, {})
    plain = default_registry().get("stripes").compute(strip, MOWER, {"angle_deg": 0})
    assert len(auto.segments) < len(plain.segments)


def test_alternating_rotates_and_is_reproducible_when_random() -> None:
    rotate = [
        alternating_angle(AlternatingSettings(run_index=i, angles_deg=[0, 60, 120]))
        for i in range(4)
    ]
    assert rotate == [0.0, 60.0, 120.0, 0.0]
    first = alternating_angle(AlternatingSettings(mode="random", seed=7, run_index=3))
    again = alternating_angle(AlternatingSettings(mode="random", seed=7, run_index=3))
    other = [
        alternating_angle(AlternatingSettings(mode="random", seed=7, run_index=i)) for i in range(8)
    ]
    assert first == again
    assert 0 <= first < 180
    assert len(set(other)) > 1


def test_alternating_rejects_angles_out_of_range() -> None:
    with pytest.raises(InvalidSettings):
        BUILTIN_PATTERNS["alternating"]().parse({"angles_deg": [400]})


def test_cells_decompose_an_l_shape_into_rectangles() -> None:
    l_area = SHAPES["l_shape"]()
    cells = decompose(l_area.region())
    assert len(cells) == 2
    assert sum(c.area for c in cells) == pytest.approx(l_area.region().area)
    holed_cells = decompose(holed().region())
    assert len(holed_cells) == 4


def test_spiral_outward_reverses_inward() -> None:
    area = front_area()
    inward = default_registry().get("spiral").compute(area, MOWER, {"direction": "inward"})
    outward = default_registry().get("spiral").compute(area, MOWER, {"direction": "outward"})
    assert inward.mow_length_m == pytest.approx(outward.mow_length_m, rel=1e-6)
    assert inward.segments[0].start == pytest.approx(outward.segments[-1].end)


def test_contour_rings_limit_the_laps() -> None:
    area = front_area()
    full = default_registry().get("contour").compute(area, MOWER, {})
    two = default_registry().get("contour").compute(area, MOWER, {"rings": 2})
    assert sum(1 for s in two.segments if s.kind == "mow") == 2
    assert two.covered_area_m2 < full.covered_area_m2


def test_border_fill_runs_laps_before_the_fill() -> None:
    area = front_area()
    plan = (
        default_registry()
        .get("border_fill")
        .compute(
            area, MOWER, {"laps": 2, "fill": {"pattern": "diagonal", "settings": {"angle_deg": 10}}}
        )
    )
    mow = [s for s in plan.segments if s.kind == "mow"]
    assert mow[0].start == mow[0].end  # a closed lap first
    assert mow[1].start == mow[1].end
    assert check(plan, area, MOWER) == []


def test_border_fill_reports_invalid_fill_settings() -> None:
    with pytest.raises(InvalidSettings) as info:
        BUILTIN_PATTERNS["border_fill"]().parse(
            {"fill": {"pattern": "stripes", "settings": {"x": 1}}}
        )
    assert info.value.errors[0].startswith("fill.settings.")


def test_remow_mows_only_the_gaps() -> None:
    area = front_area()
    gap = [(2.0, 2.0), (3.0, 2.0), (3.0, 3.0), (2.0, 3.0)]
    plan = default_registry().get("remow").compute(area, MOWER, {"gaps": [gap]})
    covered = Polygon(gap).intersection(
        __import__("mowdeck_patterns.geometry", fromlist=["covered_geometry"]).covered_geometry(
            plan.segments, MOWER.cutting_width_m
        )
    )
    assert covered.area == pytest.approx(1.0, rel=0.01)
    assert plan.covered_area_m2 < 4.0


def test_remow_from_covered_ground_mows_the_rest() -> None:
    area = front_area()
    plan = (
        default_registry()
        .get("remow")
        .compute(area, MOWER, {"covered": [[(0, 0), (12, 0), (12, 4), (0, 4)]]})
    )
    for segment in plan.segments:
        if segment.kind == "mow":
            assert min(y for _, y in segment.polyline) > 3.5


def test_remow_needs_gaps_or_coverage() -> None:
    with pytest.raises(InvalidSettings):
        BUILTIN_PATTERNS["remow"]().parse({})


def test_remow_warns_about_unreachable_gaps() -> None:
    area = front_area()
    edge_gap = [(11.95, 0.5), (12.0, 0.5), (12.0, 1.5), (11.95, 1.5)]
    middle_gap = [(5.0, 4.0), (6.0, 4.0), (6.0, 5.0), (5.0, 5.0)]
    spec = MowerSpec(cutting_width_m=0.4, overlap_m=0.05, safety_margin_m=0.6)
    plan = (
        default_registry()
        .get("remow")
        .compute(area, spec, {"gaps": [edge_gap, middle_gap], "min_gap_m2": 0.01})
    )
    assert plan.warnings and "too close" in plan.warnings[0]


def test_unknown_settings_are_refused() -> None:
    with pytest.raises(InvalidSettings) as info:
        default_registry().get("stripes").compute(front_area(), MOWER, {"angel": 3})
    assert "angel" in str(info.value)


def test_too_narrow_area_cannot_be_planned() -> None:
    sliver = Area.of([(0, 0), (5, 0), (5, 0.2), (0, 0.2)])
    with pytest.raises(PlanningError):
        default_registry().get("stripes").compute(sliver, MOWER, {})


@pytest.mark.parametrize(
    ("key", "settings", "expected"),
    [
        ("stripes", {"angle_deg": 190}, {"name": "stripes", "angle_deg": 10.0}),
        ("diagonal", {"angle_deg": 0}, {"name": "diagonal", "angle_deg": 0.0}),
        ("checkerboard", {}, {"name": "checkerboard", "angle_deg": 0.0}),
        ("edge_only", {"laps": 2}, {"name": "edge_only", "border_laps": 2}),
        ("alternating", {"run_index": 1}, {"name": "stripes", "angle_deg": 45.0}),
        (
            "border_fill",
            {"laps": 1, "fill": {"pattern": "stripes", "settings": {"angle_deg": 30}}},
            {"name": "stripes", "angle_deg": 30.0, "border_laps": 1, "order": "border_first"},
        ),
    ],
)
def test_native_jobs_translate_to_the_mower(
    key: str, settings: dict[str, Any], expected: dict[str, Any]
) -> None:
    pattern = BUILTIN_PATTERNS[key]()
    job = pattern.native_job(settings, MOWER, front_area())
    assert job is not None
    assert job.items() >= expected.items()
    caps = MowerCapabilities(
        native_patterns=frozenset({"stripes", "diagonal", "checkerboard", "edge_only"})
    )
    native = plan_to_native_job(
        MowRequest.from_start_mow(StartMow(area_ids=["front"], pattern=job)), caps
    )
    assert native.pattern == expected["name"]


@pytest.mark.parametrize(
    ("key", "settings"),
    [
        ("spiral", {}),
        ("contour", {}),
        ("cells", {}),
        ("remow", {"gaps": [[(1, 1), (2, 1), (2, 2)]]}),
        ("edge_only", {"laps": 5}),
        ("border_fill", {"laps": 5}),
        ("border_fill", {"fill": {"pattern": "spiral"}}),
    ],
)
def test_patterns_without_native_form_say_so(key: str, settings: dict[str, Any]) -> None:
    assert BUILTIN_PATTERNS[key]().native_job(settings, MOWER, front_area()) is None


def test_auto_aligned_native_job_needs_the_area() -> None:
    pattern = BUILTIN_PATTERNS["auto_aligned"]()
    assert pattern.native_job({}, MOWER) is None
    job = pattern.native_job({}, MOWER, front_area())
    assert job is not None
    assert job["name"] == "stripes"


Factory = Callable[[], Area]
