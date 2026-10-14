"""Recurrence: weekly and every N days, in the site's time zone, across DST changes."""

from datetime import UTC, date, datetime, time, timedelta
from uuid import uuid4
from zoneinfo import ZoneInfo

import pytest

from scheduler.application.runner import compute_next
from scheduler.domain.errors import InvalidInput
from scheduler.domain.models import Schedule
from scheduler.domain.recurrence import Recurrence, local_to_utc, next_after, upcoming
from scheduler.domain.rules import pattern_for_run, validate_schedule, window_end

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
    second = next_after(rule, BERLIN, first)
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
        {"timezone_override": "Mars/Base"},
        {"rain": {"probability_threshold": 101}},
        {"rain": {"precipitation_mm_threshold": -1}},
        {"rain": {"lookahead_hours": 25}},
    ]
    for values in bad:
        with pytest.raises(InvalidInput):
            validate_schedule(_schedule(**values))


def test_scheduler_rotation_cycles_through_the_angles() -> None:
    angles = [pattern_for_run({"spacing": 1}, [0, 45, 90], i)["angle_deg"] for i in range(5)]
    assert angles == [0, 45, 90, 0, 45]
    assert "angle" not in pattern_for_run({"angle": 10}, [0, 45], 1)


def test_scheduler_run_pattern_is_mow_pattern_shaped() -> None:
    # No settings at all: stripes at 0 degrees with one border lap (the edges get mowed).
    assert pattern_for_run(None, [], 3) == {"name": "stripes", "angle_deg": 0.0, "border_laps": 1}
    # The legacy key "angle" becomes angle_deg; other keys are kept.
    assert pattern_for_run({"angle": 200, "spacing": 1}, [], 0) == {
        "name": "stripes",
        "angle_deg": 20.0,
        "border_laps": 1,
        "spacing": 1,
    }
    assert pattern_for_run(None, [-45], 0)["angle_deg"] == 135.0


def test_scheduler_run_pattern_merges_the_mower_defaults() -> None:
    defaults = {"name": "diagonal", "angle_deg": 30.0, "border_laps": 2, "spacing_m": 0.3}
    # Without an angle of its own the schedule runs the defaults unchanged.
    assert pattern_for_run(None, [], 0, defaults) == defaults
    # The schedule only sets the angle.
    assert pattern_for_run({"angle": 90}, [], 0, defaults) == {**defaults, "angle_deg": 90.0}
    assert pattern_for_run(None, [0, 120], 1, defaults) == {**defaults, "angle_deg": 120.0}
