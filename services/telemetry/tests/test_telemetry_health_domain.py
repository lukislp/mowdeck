"""Health history accounting (pure functions of ``telemetry.domain.health``)."""

from collections.abc import Iterable
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

from mowdeck_contracts.v1 import MowerActivity, MowerState
from telemetry_support import T0, make_state, seconds

from telemetry.domain.health import (
    BladeChange,
    DailyRow,
    Delta,
    HealthLimits,
    HealthRow,
    advance,
    apply_delta,
    device_seconds,
    haversine_m,
    reset_blade,
    utc_day,
)

MOWER, ACCOUNT = uuid4(), uuid4()
LIMITS = HealthLimits(
    max_gap=timedelta(seconds=120), max_step_m=25.0, blade_threshold_seconds=3600.0
)
MOWING, DOCKED, ERROR = MowerActivity.MOWING, MowerActivity.DOCKED, MowerActivity.ERROR


def state(offset: float, activity: MowerActivity = DOCKED, **extra: Any) -> MowerState:
    """A state ``offset`` seconds after ``T0``."""
    return make_state(MOWER, ACCOUNT, at=T0 + seconds(offset), activity=activity, **extra)


def run(
    states: Iterable[MowerState], limits: HealthLimits = LIMITS, row: HealthRow | None = None
) -> tuple[HealthRow, dict[Any, DailyRow], int]:
    """Apply states in order; returns the totals, the day rows and the threshold crossings."""
    days: dict[Any, DailyRow] = {}
    crossings = 0
    current = row
    for s in states:
        step = advance(current, s, limits, s.observed_at)
        if step is None:
            continue
        current = step.row
        crossings += step.threshold_crossed
        for delta in step.deltas:
            days[delta.day] = apply_delta(days.get(delta.day), delta, MOWER, ACCOUNT)
    assert current is not None
    return current, days, crossings


def test_telemetry_health_blade_hours_follow_the_raw_flag_of_the_interval_start() -> None:
    row, _, _ = run(
        [
            state(0, MOWING, raw={"blade_on": True}),
            state(10, MOWING, raw={"blade_on": False}),  # transfer: blades off
            state(20, MOWING, raw={"blade_on": True}),
            state(30, MowerActivity.IDLE, raw={"blade_on": False}),
            state(40, MowerActivity.IDLE, raw={"blade_on": False}),
        ]
    )
    assert row.blade_seconds == 20  # [0, 10) and [20, 30)
    assert row.mowing_seconds == 30  # [0, 30) was activity mowing
    assert row.last_blade_on is False


def test_telemetry_health_blade_hours_without_the_flag_assume_blades_while_mowing() -> None:
    row, _, _ = run(
        [
            state(0, DOCKED),
            state(10, MOWING),
            state(20, MOWING, raw={"blade_on": "yes"}),  # not a boolean: ignored
            state(50, DOCKED),
            state(60, DOCKED),
        ]
    )
    assert row.blade_seconds == 40 and row.mowing_seconds == 40
    assert row.last_blade_on is None and row.since == T0


def test_telemetry_health_gap_longer_than_the_cap_counts_nothing() -> None:
    row, _, _ = run(
        [
            state(0, MOWING),
            state(100, MOWING, position=(48.0, 11.0)),  # 100 s: counted
            state(400, MOWING, position=(48.0001, 11.0)),  # 300 s silent: nothing
            state(410, DOCKED),
        ]
    )
    assert row.mowing_seconds == 110 and row.blade_seconds == 110
    assert row.distance_m == 0  # the only step crossed the gap


def test_telemetry_health_distance_skips_rtk_jumps_and_missing_positions() -> None:
    step = haversine_m(48.0, 11.0, 48.0001, 11.0)
    assert 11.0 < step < 11.2  # about 11.1 m per 0.0001 degree latitude
    row, _, _ = run(
        [
            state(0, MOWING, position=(48.0, 11.0)),
            state(10, MOWING, position=(48.0001, 11.0)),  # +11 m
            state(20, MOWING, position=(48.0011, 11.0)),  # +111 m: jump, skipped
            state(30, MOWING, position=(48.0012, 11.0)),  # +11 m from the jumped point
            state(40, MOWING),  # no position: the chain breaks
            state(50, MOWING, position=(48.0013, 11.0)),
        ]
    )
    assert abs(row.distance_m - 2 * step) < 0.01


def test_telemetry_health_battery_cycles_need_low_then_high() -> None:
    batteries = [90, 50, 19, 40, 80, 81, 85, 30, 10, 95, 15]
    row, _, _ = run(state(i * 10, battery=b) for i, b in enumerate(batteries))
    assert row.battery_cycles == 2  # 19 -> 81 and 10 -> 95; 15 arms again
    assert row.battery_armed is True


def test_telemetry_health_charge_sessions_count_transitions_into_charging() -> None:
    row, _, _ = run(
        [
            state(0, MowerActivity.CHARGING),  # first state charging: a session
            state(10, DOCKED, charging=True),  # still charging
            state(20, DOCKED),
            state(30, DOCKED, charging=True),  # a new session
            state(40, MOWING),
            state(50, MowerActivity.CHARGING),
        ]
    )
    assert row.charge_sessions == 3 and row.last_charging is True


def test_telemetry_health_errors_count_entries_and_code_changes() -> None:
    row, _, _ = run(
        [
            state(0, MOWING),
            state(10, ERROR, error_code="E12"),
            state(20, ERROR, error_code="E12"),  # same error, still in it
            state(30, ERROR, error_code="E7"),  # code changed
            state(40, ERROR),  # no code: unknown
            state(50, MOWING, error_code="E12"),  # a code outside error: ignored
            state(60, ERROR, error_code="E12"),
        ]
    )
    assert row.errors == {"E12": 2, "E7": 1, "unknown": 1}


def test_telemetry_health_days_are_utc_and_intervals_belong_to_their_start() -> None:
    late = datetime(2026, 10, 9, 23, 59, 30, tzinfo=UTC)
    states = [
        make_state(MOWER, ACCOUNT, at=late, activity=MOWING, battery=10),
        make_state(MOWER, ACCOUNT, at=late + seconds(60), activity=MOWING, battery=90),
        make_state(MOWER, ACCOUNT, at=late + seconds(70), activity=DOCKED, battery=90),
    ]
    row, days, _ = run(states)
    first, second = utc_day(late), utc_day(late + seconds(60))
    assert sorted(days) == [first, second]
    assert days[first].mowing_seconds == 60  # the whole interval crossing midnight
    assert days[first].battery_cycles == 0
    assert days[second].mowing_seconds == 10 and days[second].battery_cycles == 1
    assert row.mowing_seconds == 70 and row.battery_cycles == 1


def test_telemetry_health_ignores_equal_and_older_states() -> None:
    row, _, _ = run([state(0, MOWING), state(10, MOWING)])
    assert advance(row, state(10, MOWING), LIMITS, T0) is None  # redelivery
    assert advance(row, state(5, ERROR), LIMITS, T0) is None  # out of order
    again, _, _ = run([state(10, MOWING), state(10, ERROR), state(5, DOCKED)], row=row)
    assert again == row


def test_telemetry_health_threshold_is_crossed_once_until_the_blade_reset() -> None:
    mowing = [state(i * 100, MOWING) for i in range(40)]  # 3900 s of blades
    row, _, crossings = run(mowing)
    assert crossings == 1 and row.blade_threshold_notified is True
    changed = T0 + seconds(4000)
    reset = reset_blade(row, changed)
    assert (reset.blade_seconds_since_change, reset.blade_changed_at) == (0, changed)
    assert reset.blade_threshold_notified is False and reset.blade_seconds == row.blade_seconds
    later = [state(4000 + i * 100, MOWING) for i in range(40)]
    after, _, again = run(later, row=reset)
    assert again == 1 and after.blade_seconds_since_change == 4000  # incl. [3900, 4000)
    assert after.blade_seconds == row.blade_seconds + 4000


def test_telemetry_health_apply_delta_merges_errors() -> None:
    day = utc_day(T0)
    first = apply_delta(None, Delta(day, errors={"E1": 1}, distance_m=2.5), MOWER, ACCOUNT)
    second = apply_delta(
        first, Delta(day, errors={"E1": 2, "E2": 1}, battery_cycles=1), MOWER, ACCOUNT
    )
    assert second.errors == {"E1": 3, "E2": 1} and first.errors == {"E1": 1}
    assert (second.distance_m, second.battery_cycles, second.day) == (2.5, 1, day)
    change = BladeChange(uuid4(), MOWER, ACCOUNT, T0)
    assert change.note is None and change.changed_by is None


def test_telemetry_health_jobs_count_starts_of_mowing_not_resumes() -> None:
    paused, offline = MowerActivity.PAUSED, MowerActivity.OFFLINE
    _, days, _ = run(
        [
            state(0, DOCKED),
            state(10, MOWING),  # job 1
            state(20, MOWING),
            state(30, paused),
            state(40, MOWING),  # a resume: still job 1
            state(50, MowerActivity.RETURNING),
            state(60, DOCKED),
            state(70, MOWING),  # job 2
            state(80, offline),
            state(90, MOWING),  # after offline: job 3
        ]
    )
    assert days[utc_day(T0)].jobs == 3


def test_telemetry_health_first_state_mowing_is_a_job() -> None:
    _, days, _ = run([state(0, MOWING), state(10, MOWING)])
    assert days[utc_day(T0)].jobs == 1


def test_telemetry_health_online_time_skips_offline_intervals_and_gaps() -> None:
    _, days, _ = run(
        [
            state(0, DOCKED),
            state(10, DOCKED),  # +10 online
            state(20, MowerActivity.OFFLINE),  # +10 (interval started docked)
            state(50, DOCKED),  # interval started offline: 0
            state(60, MOWING),  # +10
            state(500, DOCKED),  # gap of 440 s > 120 s: 0
            state(510, DOCKED),  # +10
        ]
    )
    day = days[utc_day(T0)]
    assert day.online_seconds == 40
    assert day.mowing_seconds == 0  # the mowing interval was a gap


def test_telemetry_health_apply_delta_adds_jobs_and_online_time() -> None:
    day = utc_day(T0)
    first = apply_delta(None, Delta(day, jobs=1, online_seconds=12.5), MOWER, ACCOUNT)
    second = apply_delta(first, Delta(day, jobs=2, online_seconds=7.5), MOWER, ACCOUNT)
    assert (second.jobs, second.online_seconds) == (3, 20.0)


def test_telemetry_health_keeps_the_newest_device_blade_counter() -> None:
    row, _, _ = run([state(0), state(10)])
    assert row.device_blade_seconds is None and row.device_blade_warn_seconds is None
    row, _, _ = run(
        [
            state(20, raw={"blade_used_s": 3600, "blade_used_warn_s": 360000}),
            state(30, raw={"blade_used_s": 3700}),  # no threshold: the stored one stays
            state(40),  # no counter at all: both stay
        ],
        row=row,
    )
    assert (row.device_blade_seconds, row.device_blade_warn_seconds) == (3700, 360000)
    assert row.blade_seconds == 0  # the own counter is independent (docked, no blades)
    older = advance(row, state(35, raw={"blade_used_s": 1}), LIMITS, T0)
    assert older is None  # an older state never overwrites the counter


def test_telemetry_health_device_seconds_accepts_positive_numbers_only() -> None:
    assert device_seconds({"x": 12}, "x") == 12.0
    assert device_seconds({"x": 1.5}, "x") == 1.5
    for bad in (0, -3, True, "12", None, float("nan"), float("inf")):
        assert device_seconds({"x": bad}, "x") is None
    assert device_seconds({}, "x") is None
