"""Recurrence: weekly and every N days, in the site's time zone, across DST changes."""

from datetime import UTC, date, datetime, time, timedelta
from uuid import uuid4
from zoneinfo import ZoneInfo

import pytest
from mowdeck_contracts.v1 import MowerCapabilities

from scheduler.application.runner import compute_next
from scheduler.domain.errors import InvalidInput
from scheduler.domain.models import Schedule
from scheduler.domain.recurrence import Recurrence, local_to_utc, next_after, upcoming
from scheduler.domain.rules import (
    mower_problem,
    pattern_for_run,
    run_settings,
    schedule_pattern,
    sets_mower_options,
    validate_schedule,
    window_end,
)

BERLIN = ZoneInfo("Europe/Berlin")


def utc(year: int, month: int, day: int, hour: int = 0, minute: int = 0) -> datetime:
    return datetime(year, month, day, hour, minute, tzinfo=UTC)


def weekly(at: time, *days: int) -> Recurrence:
    return Recurrence(kind="weekly", at=at, days=frozenset(days))


def test_scheduler_weekly_keeps_the_wall_clock_across_the_autumn_change() -> None:
    rule = weekly(time(7, 0), 5, 6)  # Saturday and Sunday 07:00
    found = upcoming(rule, BERLIN, utc(2026, 10, 20), 4)
    local = [t.astimezone(BERLIN) for t in found]
    assert [t.strftime("%a %d %H:%M") for t in local] == [
        "Sat 24 07:00",
        "Sun 25 07:00",
        "Sat 31 07:00",
        "Sun 01 07:00",
    ]
    # 05:00 UTC in summer time, 06:00 UTC after the change on Sunday 25 October 2026.
    assert [t.hour for t in found] == [5, 6, 6, 6]


def test_scheduler_spring_gap_fires_one_hour_later_on_the_same_day() -> None:
    rule = weekly(time(2, 30), 6)  # Sunday 02:30 does not exist on 28 March 2027
    found = next_after(rule, BERLIN, utc(2027, 3, 27, 12))
    assert found == utc(2027, 3, 28, 1, 30)
    assert found.astimezone(BERLIN).strftime("%Y-%m-%d %H:%M %Z") == "2027-03-28 03:30 CEST"
    after = next_after(rule, BERLIN, found)
    assert after is not None and after.astimezone(BERLIN).strftime("%H:%M") == "02:30"


def test_scheduler_autumn_overlap_fires_once_at_the_first_occurrence() -> None:
    rule = weekly(time(2, 30), 6)  # Sunday 02:30 exists twice on 25 October 2026
    first = next_after(rule, BERLIN, utc(2026, 10, 24, 12))
    assert first == utc(2026, 10, 25, 0, 30)  # 02:30 CEST
    assert first is not None
    second = next_after(rule, BERLIN, first, last_fired=first)
    assert second is not None and second.date() == date(2026, 11, 1)  # not 01:30 UTC again


def test_scheduler_every_n_days_counts_calendar_days_from_the_anchor() -> None:
    rule = Recurrence(
        kind="every_n_days", at=time(9, 0), anchor=date(2026, 10, 20), interval_days=3
    )
    found = upcoming(rule, BERLIN, utc(2026, 10, 1), 4)
    local = [t.astimezone(BERLIN) for t in found]
    assert [t.date() for t in local] == [
        date(2026, 10, 20),
        date(2026, 10, 23),
        date(2026, 10, 26),  # across the DST change: still 09:00 local
        date(2026, 10, 29),
    ]
    assert {t.strftime("%H:%M") for t in local} == {"09:00"}
    assert found[2] - found[1] == timedelta(hours=73)


def test_scheduler_every_n_days_with_a_past_anchor_continues_the_rhythm() -> None:
    rule = Recurrence(kind="every_n_days", at=time(6, 0), anchor=date(2026, 1, 1), interval_days=7)
    found = next_after(rule, BERLIN, utc(2026, 10, 9, 12))
    assert found is not None
    assert (found.astimezone(BERLIN).date() - date(2026, 1, 1)).days % 7 == 0
    assert found.astimezone(BERLIN).date() == date(2026, 10, 15)


def test_scheduler_once_fires_a_single_time_on_its_day() -> None:
    rule = Recurrence(kind="once", at=time(7, 30), anchor=date(2026, 11, 2))
    first = next_after(rule, BERLIN, utc(2026, 11, 1, 12))
    assert first == utc(2026, 11, 2, 6, 30)
    assert first is not None
    assert next_after(rule, BERLIN, first, last_fired=first) is None
    assert next_after(rule, BERLIN, utc(2026, 11, 3)) is None
    assert next_after(Recurrence(kind="once", at=time(7, 0)), BERLIN, utc(2026, 1, 1)) is None


def test_scheduler_once_validation_needs_its_day() -> None:
    item = _schedule(kind="once", anchor_date=date(2026, 11, 2), interval_days=4)
    validate_schedule(item)
    assert item.days_of_week == [] and item.interval_days is None
    assert item.recurrence.kind == "once"
    with pytest.raises(InvalidInput):
        validate_schedule(_schedule(kind="once"))


def test_scheduler_a_rule_that_never_fires_has_no_next_run() -> None:
    assert next_after(weekly(time(7, 0)), BERLIN, utc(2026, 1, 1)) is None
    broken = Recurrence(kind="every_n_days", at=time(7, 0))
    assert next_after(broken, BERLIN, utc(2026, 1, 1)) is None


def test_scheduler_next_is_strictly_after_and_the_same_day_counts() -> None:
    rule = weekly(time(10, 30), 4)  # Friday
    assert next_after(rule, BERLIN, utc(2026, 10, 9, 8, 0)) == utc(2026, 10, 9, 8, 30)
    assert next_after(rule, BERLIN, utc(2026, 10, 9, 8, 30)) == utc(2026, 10, 16, 8, 30)
    assert local_to_utc(date(2026, 7, 1), time(12, 0), BERLIN) == utc(2026, 7, 1, 10, 0)


def test_scheduler_other_time_zones_and_window_end() -> None:
    rule = weekly(time(6, 0), 0)
    found = next_after(rule, ZoneInfo("America/New_York"), utc(2026, 11, 1))
    assert found == utc(2026, 11, 2, 11, 0)  # EST after 1 November 2026
    assert window_end(found, 90) == utc(2026, 11, 2, 12, 30)


def _schedule(**values: object) -> Schedule:
    now = utc(2026, 10, 9)
    base: dict[str, object] = {
        "id": uuid4(),
        "account_id": uuid4(),
        "name": " Lawn ",
        "target_type": "mower",
        "target_id": uuid4(),
        "kind": "weekly",
        "start_time": time(10, 30),
        "duration_minutes": 60,
        "timezone": "Europe/Berlin",
        "owner": {"subject": str(uuid4()), "kind": "user"},
        "owner_subject": uuid4(),
        "created_at": now,
        "updated_at": now,
        "days_of_week": [4, 4, 0],
    }
    base.update(values)
    return Schedule(**base)  # type: ignore[arg-type]


def test_scheduler_validation_normalises_and_refuses() -> None:
    item = _schedule()
    validate_schedule(item)
    assert item.name == "Lawn" and item.days_of_week == [0, 4]
    assert compute_next(item, utc(2026, 10, 9, 9)) == utc(2026, 10, 12, 8, 30)
    every = _schedule(kind="every_n_days", anchor_date=date(2026, 10, 1), interval_days=2)
    validate_schedule(every)
    assert every.days_of_week == []
    bad: list[dict[str, object]] = [
        {"name": "  "},
        {"target_type": "site"},
        {"days_of_week": []},
        {"days_of_week": [7]},
        {"kind": "every_n_days"},
        {"kind": "every_n_days", "anchor_date": date(2026, 1, 1), "interval_days": 400},
        {"kind": "monthly"},
        {"duration_minutes": 0},
        {"target_type": "group", "area_ids": ["front"]},
        {"area_ids": [""]},
        {"rotation_angles": [float("nan")]},
        {"pattern": {"x": "y" * 5000}},
        {"pattern": {"border_laps": 9}},
        {"blade_height_mm": 5},
        {"speed_mps": 0},
        {"speed_mps": 2.5},
        {"timezone_override": "Mars/Base"},
        {"rain": {"probability_threshold": 101}},
        {"rain": {"precipitation_mm_threshold": -1}},
        {"rain": {"lookahead_hours": 25}},
    ]
    for values in bad:
        with pytest.raises(InvalidInput):
            validate_schedule(_schedule(**values))


def test_scheduler_rotation_cycles_through_the_angles() -> None:
    angles = [pattern_for_run({"border_laps": 2}, [0, 45, 90], i)["angle_deg"] for i in range(5)]
    assert angles == [0, 45, 90, 0, 45]
    # The rotation replaces the schedule's own angle.
    assert pattern_for_run({"angle_deg": 10}, [0, 45], 1)["angle_deg"] == 45.0


def test_scheduler_run_pattern_is_mow_pattern_shaped() -> None:
    # No settings at all: stripes at 0 degrees with one border lap (the edges get mowed).
    assert pattern_for_run(None, [], 3) == {"name": "stripes", "angle_deg": 0.0, "border_laps": 1}
    assert pattern_for_run(None, [-45], 0)["angle_deg"] == 135.0
    assert pattern_for_run(None, [200], 0)["angle_deg"] == 20.0


def test_scheduler_run_pattern_merges_the_mower_defaults() -> None:
    defaults = {"name": "diagonal", "angle_deg": 30.0, "border_laps": 2, "spacing_m": 0.3}
    # Without settings of its own the schedule runs the defaults unchanged.
    assert pattern_for_run(None, [], 0, defaults) == defaults
    assert pattern_for_run({"angle_deg": 90}, [], 0, defaults) == {**defaults, "angle_deg": 90.0}
    assert pattern_for_run(None, [0, 120], 1, defaults) == {**defaults, "angle_deg": 120.0}


def test_scheduler_run_pattern_precedence_is_schedule_then_defaults_then_mower() -> None:
    defaults = {
        "name": "diagonal",
        "angle_deg": 30.0,
        "border_laps": 2,
        "obstacle_detection": "standard",
        "order": "border_first",
        "start_progress_pct": 40,
    }
    own = {"name": "checkerboard", "border_laps": 0, "obstacle_detection": "sensitive"}
    run = pattern_for_run(own, [], 0, defaults)
    assert run == {
        "name": "checkerboard",  # schedule
        "angle_deg": 30.0,  # defaults
        "border_laps": 0,  # schedule (a set 0 is not "unset")
        "obstacle_detection": "sensitive",  # schedule
        "order": "border_first",  # defaults
    }
    # Nobody sets the spacing or the no-go laps: left out, the mower keeps its own; the start
    # progress of the defaults never reaches a scheduled run.
    assert "spacing_m" not in run and "obstacle_laps" not in run
    assert "start_progress_pct" not in run


def test_scheduler_run_pattern_carries_the_job_options_of_the_defaults() -> None:
    # The job options saved with the mowing defaults reach every scheduled start (ADR 0030).
    options = {"obstacle_detection": "sensitive", "obstacle_laps": 0, "order": "grid_first"}
    defaults = {"name": "stripes", "angle_deg": 0.0, "border_laps": 1, **options}
    run = pattern_for_run({"angle_deg": 45}, [], 0, defaults)
    assert {key: run[key] for key in options} == options
    assert run["angle_deg"] == 45.0


AWD_CAPS = MowerCapabilities.model_validate(
    {
        "native_patterns": ["stripes", "diagonal"],
        "path_spacing_range_mm": [200, 350],
        "blade_height_range_mm": [25, 70],
        "min_speed_mps": 0.2,
        "max_speed_mps": 1.0,
        "start_options": {
            "obstacle_detection": ["off", "standard", "sensitive"],
            "obstacle_laps_range": [0, 4],
            "orders": ["border_first", "grid_first"],
        },
    }
)
BARE_CAPS = MowerCapabilities.model_validate({"native_patterns": ["stripes"]})


def test_scheduler_schedule_pattern_is_normalised_and_checked() -> None:
    item = _schedule(pattern={"angle_deg": 45, "obstacle_detection": None})
    validate_schedule(item)
    assert item.pattern == {"angle_deg": 45.0}
    empty = _schedule(pattern={})
    validate_schedule(empty)
    assert empty.pattern is None
    assert schedule_pattern({"name": "diagonal", "unknown": 1}) == {"name": "diagonal"}


def test_scheduler_mower_problem_follows_the_capabilities() -> None:
    plain = _schedule(pattern={"angle_deg": 90, "border_laps": 3})
    # Angle and border laps fit every mower, even one whose capabilities are unknown.
    assert not sets_mower_options(plain)
    assert mower_problem(plain, None) is None
    full = _schedule(
        pattern={"name": "diagonal", "spacing_m": 0.25, "obstacle_detection": "sensitive"},
        blade_height_mm=40,
        speed_mps=0.6,
    )
    assert mower_problem(full, AWD_CAPS) is None
    assert mower_problem(full, None) == "its capabilities are not known yet"
    assert mower_problem(full, BARE_CAPS) == "pattern 'diagonal' is not offered"
    assert "blade height is not settable" in (
        mower_problem(_schedule(blade_height_mm=40), BARE_CAPS) or ""
    )
    assert "between 25 and 70" in (mower_problem(_schedule(blade_height_mm=80), AWD_CAPS) or "")
    assert "speed is not settable" in (mower_problem(_schedule(speed_mps=0.5), BARE_CAPS) or "")
    assert "between 0.2 and 1" in (mower_problem(_schedule(speed_mps=1.5), AWD_CAPS) or "")


def test_scheduler_run_settings_fit_the_mower_and_send_setpoints_first() -> None:
    schedule = _schedule(
        pattern={"name": "stripes", "spacing_m": 0.3, "order": "grid_first"},
        blade_height_mm=40,
        speed_mps=0.6,
    )
    defaults = {"obstacle_detection": "sensitive", "obstacle_laps": 2}
    fitted = run_settings(schedule, defaults, AWD_CAPS)
    assert fitted.pattern == {
        "name": "stripes",
        "angle_deg": 0.0,
        "border_laps": 1,
        "spacing_m": 0.3,
        "order": "grid_first",
        "obstacle_detection": "sensitive",
        "obstacle_laps": 2,
    }
    assert [c.model_dump() for c in fitted.setpoints] == [
        {"kind": "set_blade_height", "mm": 40},
        {"kind": "set_speed", "mps": 0.6},
    ]
    assert fitted.dropped == []
    # A mower that offers none of it (it changed, or joined the group later): left out.
    bare = run_settings(schedule, defaults, BARE_CAPS)
    assert bare.pattern == {"name": "stripes", "angle_deg": 0.0, "border_laps": 1}
    assert bare.setpoints == []
    assert bare.dropped == [
        "obstacle_detection",
        "obstacle_laps",
        "order",
        "spacing_m",
        "blade_height_mm",
        "speed_mps",
    ]
    # Unknown capabilities: everything goes out as set, the controller decides.
    unknown = run_settings(schedule, defaults, None)
    assert unknown.pattern["order"] == "grid_first" and len(unknown.setpoints) == 2
