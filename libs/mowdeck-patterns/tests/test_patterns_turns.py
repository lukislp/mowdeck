"""Turn shapes at lane ends (ADR 0043): arcs, bulb turns, three-point turns and fallbacks."""

import math

import pytest
from mowdeck_patterns import (
    Area,
    MowerSpec,
    MowPlan,
    PatternError,
    TurnOptions,
    check,
    plan,
    preview,
    shape_turns,
)
from mowdeck_patterns.models import Segment
from mowdeck_patterns.turns import REVERSAL_DEG, arc_shape, multi_point_shape
from patterns_support import MOWER, front_area, holed
from shapely.geometry import Polygon, box

STRIPES = [("stripes", {"angle_deg": 0})]


def _corners(
    segments: list[Segment] | tuple[Segment, ...], skip: tuple[Segment, ...] = ()
) -> list[float]:
    """Heading changes (degrees) at every vertex of every continuous forward or reverse piece
    of mow and turn segments (``skip``: straight lane ends kept for turning in place)."""
    changes: list[float] = []
    track: list[tuple[float, float]] = []
    reverse = False
    for segment in segments:
        if segment.kind == "transfer" or segment.reverse != reverse:
            track = []
        reverse = segment.reverse
        if segment.kind == "transfer" or segment in skip:
            track = []
            continue
        for point in segment.polyline:
            if not track or math.dist(track[-1], point) > 1e-6:
                track.append(point)
            if len(track) >= 3:
                a, b, c = track[-3:]
                h1 = math.atan2(b[1] - a[1], b[0] - a[0])
                h2 = math.atan2(c[1] - b[1], c[0] - b[0])
                diff = abs(h2 - h1) % (2 * math.pi)
                changes.append(math.degrees(min(diff, 2 * math.pi - diff)))
    return changes


def _turns(result: MowPlan) -> list[Segment]:
    return [s for s in result.segments if s.kind == "turn"]


def test_without_turn_options_the_plan_keeps_its_straight_connections() -> None:
    plain = plan(front_area(), MOWER, STRIPES)
    in_place = plan(front_area(), MOWER, STRIPES, turns=TurnOptions(style="in_place"))
    assert plain.segments == in_place.segments
    assert plain.turn_style is None
    assert in_place.turn_style == "in_place"
    assert max(_corners(plain.segments)) > 80  # the mower rotates at every lane end


def test_arc_with_a_narrow_spacing_is_a_bulb_turn_without_sharp_corners() -> None:
    result = plan(front_area(), MOWER, STRIPES, turns=TurnOptions(style="arc", min_radius_m=0.3))
    assert result.turn_style == "arc"
    turns = _turns(result)
    assert turns
    assert all(not s.reverse and s.blades_on for s in turns)
    # every lane end fits, also the first and the last lane at the area's edge (lopsided
    # bulbs that swing towards the inside only)
    assert result.warnings == ()
    assert all(len(s.polyline) > 2 for s in turns)
    assert max(_corners(result.segments)) <= 16.0  # one arc step, no rotation on the spot
    assert not check(result, front_area(), MOWER)
    # a bulb swings beyond the next lane: the turn reaches outside the two lanes' band
    spacing = MOWER.spacing_m
    first = turns[0]
    ys = [p[1] for p in first.polyline]
    bulb = next(s for s in turns if len(s.polyline) > 2)
    ys = [p[1] for p in bulb.polyline]
    assert max(ys) - min(ys) > spacing + 0.1


def test_arc_with_a_wide_spacing_is_a_semicircle() -> None:
    wide = MowerSpec(cutting_width_m=0.8, overlap_m=0.05)
    result = plan(front_area(), wide, STRIPES, turns=TurnOptions(style="arc", min_radius_m=0.3))
    turn = _turns(result)[0]
    start, end = turn.polyline[0], turn.polyline[-1]
    centre = ((start[0] + end[0]) / 2, (start[1] + end[1]) / 2)
    radius = math.dist(start, end) / 2
    assert 0.3 <= radius <= wide.spacing_m / 2 + 1e-6  # half the (evened-out) lane spacing
    assert all(math.dist(p, centre) == pytest.approx(radius, abs=1e-6) for p in turn.polyline)


def test_turns_stay_inside_the_safety_margin_and_pull_back_into_the_lanes() -> None:
    area = front_area()
    plain = plan(area, MOWER, STRIPES)
    shaped = plan(area, MOWER, STRIPES, turns=TurnOptions(style="arc", min_radius_m=0.3))
    assert not check(shaped, area, MOWER)

    # the bulb does not fit beyond the lane ends: the runs are shortened instead
    def runs(p: MowPlan) -> float:
        return sum(s.length for s in p.segments if s.kind == "mow")

    assert runs(shaped) < runs(plain)
    assert shaped.covered_area_m2 > 0.9 * plain.covered_area_m2


def test_multi_point_is_a_three_point_turn_with_the_blades_off_while_reversing() -> None:
    result = plan(
        front_area(), MOWER, STRIPES, turns=TurnOptions(style="multi_point", min_radius_m=0.3)
    )
    assert result.turn_style == "multi_point"
    turns = _turns(result)
    reverse = [s for s in turns if s.reverse]
    assert reverse
    assert all(not s.blades_on for s in reverse)
    assert all(s.blades_on for s in turns if not s.reverse)
    # forward, reverse, forward at every lane end
    kinds = [("R" if s.reverse else "F") for s in result.segments if s.kind == "turn"]
    assert "".join(kinds).count("FRF") == len(reverse)
    assert max(_corners(result.segments)) <= 16.0
    assert not check(result, front_area(), MOWER)
    spot = result.to_spot_plan()
    assert any(s.reverse for s in spot.segments)
    assert [s.reverse for s in MowPlan.from_spot_plan(spot).segments] == [
        s.reverse for s in spot.segments
    ]
    assert any(f["properties"]["reverse"] for f in preview(result)["features"])


def test_multi_point_without_reverse_falls_back_to_arc() -> None:
    options = TurnOptions(style="multi_point", reverse=False)
    assert options.effective_style == "arc"
    result = plan(front_area(), MOWER, STRIPES, turns=options)
    assert result.turn_style == "arc"
    assert not any(s.reverse for s in result.segments)
    assert any("cannot reverse" in w for w in result.warnings)


def test_a_turn_that_does_not_fit_falls_back_to_turning_in_place() -> None:
    area = front_area()
    plain = plan(area, MOWER, STRIPES)
    # a fence that keeps the mower within the lanes' band: the bulb cannot swing out
    first_turn = next(s for s in plain.segments if s.kind == "turn")
    y0 = min(p[1] for p in first_turn.polyline)
    y1 = max(p[1] for p in first_turn.polyline)
    fence = box(0, y0 - 0.01, 12, y1 + 0.01)
    arc = plan(area, MOWER, STRIPES, turns=TurnOptions(style="arc", fence=fence))
    assert any("turn in place" in w for w in arc.warnings)
    assert first_turn in arc.segments  # this lane end kept its straight connection
    # the three-point turn stays between the two lanes, so it fits there
    multi = plan(area, MOWER, STRIPES, turns=TurnOptions(style="multi_point", fence=fence))
    assert first_turn not in multi.segments
    assert any(s.reverse for s in multi.segments)


def test_lane_ends_around_a_hole_and_rings_without_reversals() -> None:
    area = holed()
    result = plan(area, MOWER, STRIPES, turns=TurnOptions(style="arc"))
    assert not check(result, area, MOWER)
    rings = plan(area, MOWER, [("spiral", {})], turns=TurnOptions(style="arc"))
    plain_rings = plan(area, MOWER, [("spiral", {})])
    assert rings.segments == plain_rings.segments  # no lane-end reversals in a spiral


def test_shape_turns_counts_lane_ends() -> None:
    area = Area.of([(0, 0), (6, 0), (6, 3), (0, 3)])
    base = plan(area, MOWER, STRIPES)
    safe = Polygon([(0.16, 0.16), (5.84, 0.16), (5.84, 2.84), (0.16, 2.84)])
    result = shape_turns(base.segments, safe, TurnOptions(style="multi_point"))
    assert result.lane_ends == sum(1 for s in base.segments if s.kind == "turn")
    assert result.shaped == result.lane_ends
    assert result.in_place == []


@pytest.mark.parametrize("d", [0.2, 0.35, 0.5, 0.6, 0.9])
@pytest.mark.parametrize("shape", [arc_shape, multi_point_shape])
def test_canonical_shapes_end_on_the_next_lane_heading_back(d: float, shape: object) -> None:
    pieces = shape(d, 0.3)  # type: ignore[operator]
    end = pieces[-1][0][-1]
    assert end[1] == pytest.approx(d, abs=1e-6)
    last = pieces[-1][0]
    heading = math.degrees(math.atan2(last[-1][1] - last[-2][1], last[-1][0] - last[-2][0]))
    assert abs(abs(heading) - 180) < 180 - REVERSAL_DEG


def test_turn_options_are_checked() -> None:
    with pytest.raises(PatternError):
        TurnOptions(style="zigzag")  # type: ignore[arg-type]
    with pytest.raises(PatternError):
        TurnOptions(min_radius_m=0.0)
