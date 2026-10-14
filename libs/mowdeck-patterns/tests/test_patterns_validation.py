"""The validation chain rejects unsafe plans with reasons."""

import pytest
from mowdeck_patterns import (
    Area,
    MowerSpec,
    MowPlan,
    PlanRejected,
    Segment,
    check,
    default_registry,
    validate,
)
from mowdeck_patterns.validation import findings_summary, turn_findings
from patterns_support import MOWER, front_area


def _plan(*segments: Segment) -> MowPlan:
    return MowPlan(
        segments=segments, estimated_duration_s=1, covered_area_m2=0, pattern_chain=("t",)
    )


def test_a_plan_that_leaves_the_area_is_rejected() -> None:
    plan = _plan(Segment(((1.0, 1.0), (14.0, 1.0)), True, 0.4))
    with pytest.raises(PlanRejected) as info:
        validate(plan, front_area(), MOWER)
    assert any("leaves the area" in r for r in info.value.reasons)
    assert any("cuts outside the area" in r for r in info.value.reasons)


def test_a_plan_inside_the_area_but_within_the_margin_is_rejected() -> None:
    plan = _plan(Segment(((0.05, 1.0), (0.05, 4.0)), True, 0.4))
    reasons = check(plan, front_area(), MOWER)
    assert len(reasons) == 1
    assert "safety margin of 0.15 m" in reasons[0]


def test_blades_on_through_a_no_go_zone_is_rejected() -> None:
    plan = _plan(Segment(((8.0, 6.5), (11.0, 6.5)), True, 0.4))
    reasons = check(plan, front_area(), MOWER)
    assert any("no-go zone" in r for r in reasons)


def test_blades_on_through_a_hole_is_rejected() -> None:
    area = Area.of([(0, 0), (10, 0), (10, 10), (0, 10)], holes=[[(4, 4), (6, 4), (6, 6), (4, 6)]])
    plan = _plan(Segment(((1.0, 5.0), (9.0, 5.0)), True, 0.4))
    reasons = check(plan, area, MOWER)
    assert any("through a hole" in r for r in reasons)


def test_transfers_with_blades_on_and_excess_speed_are_rejected() -> None:
    plan = _plan(
        Segment(((1.0, 1.0), (2.0, 1.0)), True, 0.4, "transfer"),
        Segment(((2.0, 1.0), (3.0, 1.0)), True, 1.5),
        Segment(((3.0, 1.0), (4.0, 1.0)), False, 0.9, "transfer"),
    )
    reasons = check(plan, front_area(), MOWER)
    assert any("transfer with the blades on" in r for r in reasons)
    assert sum("has speed" in r for r in reasons) == 2


def test_an_empty_plan_is_rejected() -> None:
    assert check(_plan(), front_area(), MOWER) == ["the plan has no segments"]


def test_many_findings_are_summarised() -> None:
    segments = [Segment(((1.0, y), (20.0, y)), True, 0.4) for y in range(1, 8)]
    reasons = check(_plan(*segments), front_area(), MOWER)
    assert reasons[-1].startswith("... and ")
    assert len(reasons) == 13


def test_turning_radius_rejects_sharp_u_turns() -> None:
    area = Area.of([(0, 0), (20, 0), (20, 10), (0, 10)])
    wide = MowerSpec(cutting_width_m=0.4, overlap_m=0.05, turning_radius_m=0.3)
    plan = default_registry().get("stripes").compute(area, wide, {})
    reasons = check(plan, area, wide)
    assert reasons
    assert any("needs 0.60 m of straight track but has 0.34 m" in r for r in reasons)


def test_turning_radius_rejects_reversals() -> None:
    wide = MowerSpec(turning_radius_m=0.2)
    plan = _plan(Segment(((1.0, 1.0), (3.0, 1.0), (1.5, 1.0)), True, 0.4))
    findings = turn_findings(plan, wide)
    assert findings and "reversal" in findings[0]


def test_zero_radius_accepts_any_turn() -> None:
    plan = _plan(Segment(((1.0, 1.0), (3.0, 1.0), (1.5, 1.0)), True, 0.4))
    assert turn_findings(plan, MOWER) == []


def test_wide_turns_with_enough_track_pass() -> None:
    wide = MowerSpec(turning_radius_m=0.5)
    plan = _plan(
        Segment(((1.0, 1.0), (5.0, 1.0)), True, 0.4),
        Segment(((5.0, 1.0), (5.0, 3.0)), True, 0.4, "turn"),
        Segment(((5.0, 3.0), (1.0, 3.0)), True, 0.4),
        Segment(((1.0, 4.0), (2.0, 4.0)), True, 0.4),
    )
    assert turn_findings(plan, wide) == []


def test_valid_plans_pass_unchanged() -> None:
    area = front_area()
    plan = default_registry().get("stripes").compute(area, MOWER, {})
    assert validate(plan, area, MOWER) is plan
    assert findings_summary([]) == "valid"
    assert findings_summary(["a", "b"]) == "a; b"
