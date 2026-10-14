"""Coverage from a driven track, the gaps left and re-mowing them."""

import json
import math
import os
from pathlib import Path

import pytest
from mowdeck_patterns import (
    MowPlan,
    PatternError,
    Segment,
    coverage,
    covered_ratio,
    default_registry,
    gaps,
    plan,
    preview,
)
from patterns_support import MOWER, front_area

GOLDEN = Path(__file__).parent / "golden" / "front_stripes_preview.json"


def _track(result: MowPlan, step: float = 0.2) -> list[tuple[float, float]]:
    """Points every ``step`` metres along the blades-on segments, like a position log."""
    points: list[tuple[float, float]] = []
    for segment in result.segments:
        for a, b in zip(segment.polyline, segment.polyline[1:], strict=False):
            count = max(1, math.ceil(math.dist(a, b) / step))
            points.extend(
                (a[0] + (b[0] - a[0]) * i / count, a[1] + (b[1] - a[1]) * i / count)
                for i in range(count)
            )
    return points


def test_coverage_of_a_full_run_leaves_only_small_gaps() -> None:
    area = front_area()
    result = default_registry().get("stripes").compute(area, MOWER, {})
    covered = coverage(_track(result), MOWER.cutting_width_m)
    assert covered_ratio(area, covered) > 0.95
    left = gaps(area, covered, min_area=0.05)
    assert sum(g.area for g in left) < 0.05 * area.region().area


def test_half_a_run_leaves_the_other_half_and_remow_closes_it() -> None:
    area = front_area()
    result = default_registry().get("stripes").compute(area, MOWER, {})
    track = _track(result)
    covered = coverage(track[: len(track) // 2], MOWER.cutting_width_m)
    left = gaps(area, covered, min_area=0.05)
    assert left
    assert left[0].area > 30
    touch_up = plan(area, MOWER, [("remow", {"gaps": [list(g.exterior.coords) for g in left]})])
    after = coverage(track[: len(track) // 2] + _track(touch_up), MOWER.cutting_width_m)
    assert covered_ratio(area, after) > 0.95


def test_jumps_break_the_track() -> None:
    covered = coverage([(1, 1), (2, 1), (9, 1), (10, 1)], 0.4, max_jump_m=3)
    assert covered.area < 1.5
    joined = coverage([(1, 1), (2, 1), (9, 1), (10, 1)], 0.4, max_jump_m=10)
    assert joined.area > 3.5


def test_single_points_and_empty_tracks() -> None:
    assert coverage([], 0.4).is_empty
    dot = coverage([(1, 1), (math.nan, 2)], 0.4)
    assert dot.area == pytest.approx(math.pi * 0.04, rel=0.1)
    with pytest.raises(PatternError):
        coverage([(1, 1)], 0)


def test_gaps_without_coverage_are_the_whole_area() -> None:
    area = front_area()
    left = gaps(area, coverage([], 0.4))
    assert len(left) == 1
    assert left[0].area == pytest.approx(area.region().area, rel=1e-3)
    assert gaps(area.region(), coverage([], 0.4), min_width_m=0)[0].area > 80


def _local_preview() -> dict[str, object]:
    result = plan(front_area(), MOWER, [("stripes", {"angle_deg": 0})])
    return preview(result)


def test_golden_preview_of_stripes_on_the_front_area() -> None:
    current = _local_preview()
    if os.environ.get("MOWDECK_UPDATE_GOLDEN") == "1":
        GOLDEN.write_text(json.dumps(current, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    expected = json.loads(GOLDEN.read_text(encoding="utf-8"))
    assert current == expected


def test_preview_in_wgs84_uses_lon_lat() -> None:
    result = default_registry().get("edge_only").compute(front_area(), MOWER, {})
    doc = preview(result, to_wgs84=lambda x, y: (48.0 + y * 1e-5, 11.0 + x * 1e-5))
    assert doc["properties"]["crs"] == "wgs84"
    lon, lat = doc["features"][0]["geometry"]["coordinates"][0]
    assert 10.9 < lon < 11.1
    assert 47.9 < lat < 48.1


def test_preview_lists_every_segment() -> None:
    doc = preview(
        MowPlan((Segment(((0.0, 0.0), (1.0, 0.0)), False, 0.5, "transfer"),), 2, 0, ("x",))
    )
    assert doc["properties"]["segment_count"] == 1
    assert doc["features"][0]["properties"]["kind"] == "transfer"
