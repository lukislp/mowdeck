"""DST and day boundaries: next runs and rain verdicts with the clock frozen at the edges.

Europe/Berlin falls back on Sunday 25 October 2026 at 01:00 UTC (03:00 CEST becomes 02:00 CET,
so 02:00-02:59 exists twice) and springs forward on Sunday 29 March 2026 at 01:00 UTC (02:00
CET becomes 03:00 CEST, so 02:00-02:59 does not exist). See ADR 0024, addendum of 2026-10-25.
"""

from datetime import UTC, datetime, time, timedelta
from zoneinfo import ZoneInfo

import pytest
from scheduler_support import API, harness

from scheduler.domain.recurrence import Recurrence, next_after, second_occurrence, upcoming

BERLIN = ZoneInfo("Europe/Berlin")
DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")


def utc(year: int, month: int, day: int, hour: int = 0, minute: int = 0) -> datetime:
    return datetime(year, month, day, hour, minute, tzinfo=UTC)


def weekly(at: time, *days: int) -> Recurrence:
    return Recurrence(kind="weekly", at=at, days=frozenset(days))


def local(instant: datetime) -> str:
    return instant.astimezone(BERLIN).strftime("%Y-%m-%d %H:%M %Z")


# --- the recurrence ------------------------------------------------------------------------


def test_scheduler_dst_second_occurrence_only_exists_in_the_repeated_hour() -> None:
    sunday = utc(2026, 10, 25).date()
    assert second_occurrence(sunday, time(2, 30), BERLIN) == utc(2026, 10, 25, 1, 30)
    assert second_occurrence(sunday, time(3, 30), BERLIN) is None
    assert second_occurrence(sunday, time(1, 59), BERLIN) is None
    assert second_occurrence(utc(2026, 3, 29).date(), time(2, 30), BERLIN) is None  # the gap


def test_scheduler_dst_fall_back_a_schedule_made_in_the_repeated_hour_fires_today() -> None:
    # 01:28 UTC is 02:28 CET, the second 02:28 of the night. 02:29 local is one minute ahead
    # on the wall clock; its first occurrence (00:29 UTC, 02:29 CEST) is long past.
    rule = weekly(time(2, 29), 6)
    found = next_after(rule, BERLIN, utc(2026, 10, 25, 1, 28))
    assert found == utc(2026, 10, 25, 1, 29)
    assert local(found) == "2026-10-25 02:29 CET"
    # Once fired (last_fired at or after the first occurrence) the day is done.
    assert next_after(rule, BERLIN, found, last_fired=found) == utc(2026, 11, 1, 1, 29)


def test_scheduler_dst_fall_back_fires_once_when_it_fired_at_the_first_occurrence() -> None:
    rule = weekly(time(2, 30), 6)
    first = next_after(rule, BERLIN, utc(2026, 10, 24, 12))
    assert first == utc(2026, 10, 25, 0, 30)
    assert local(first) == "2026-10-25 02:30 CEST"
    # The runner computes the next run after firing, with last_run_at = the firing time;
    # a restart or an edit later in the night (still in the repeated hour) agrees.
    for after in (first, utc(2026, 10, 25, 0, 31), utc(2026, 10, 25, 1, 10)):
        assert next_after(rule, BERLIN, after, last_fired=first + timedelta(seconds=3)) == utc(
            2026, 11, 1, 1, 30
        )
    # Missed at both occurrences (Scheduler down all night): the next one is next week.
    assert next_after(rule, BERLIN, utc(2026, 10, 25, 2), last_fired=None) == utc(
        2026, 11, 1, 1, 30
    )


def test_scheduler_dst_fall_back_between_the_occurrences_takes_the_second() -> None:
    # 00:45 UTC is 02:45 CEST: 02:30 has passed once and comes again at 01:30 UTC (02:30 CET).
    rule = weekly(time(2, 30), 6)
    assert next_after(rule, BERLIN, utc(2026, 10, 25, 0, 45)) == utc(2026, 10, 25, 1, 30)
    # A schedule that already fired a different time that night is not affected.
    fired_earlier = utc(2026, 10, 24, 23)  # 01:00 CEST
    assert next_after(rule, BERLIN, utc(2026, 10, 25, 0, 45), fired_earlier) == utc(
        2026, 10, 25, 1, 30
    )


def test_scheduler_dst_fall_back_before_the_change_keeps_the_first_occurrence() -> None:
    rule = weekly(time(2, 30), 6)
    assert next_after(rule, BERLIN, utc(2026, 10, 25, 0, 10)) == utc(2026, 10, 25, 0, 30)
    found = upcoming(rule, BERLIN, utc(2026, 10, 25, 0, 10), 3)
    assert [local(t) for t in found] == [
        "2026-10-25 02:30 CEST",
        "2026-11-01 02:30 CET",
        "2026-11-08 02:30 CET",
    ]


def test_scheduler_dst_every_n_days_on_the_fall_back_day() -> None:
    rule = Recurrence(
        kind="every_n_days", at=time(2, 15), anchor=utc(2026, 10, 23).date(), interval_days=2
    )
    assert next_after(rule, BERLIN, utc(2026, 10, 25, 1, 5)) == utc(2026, 10, 25, 1, 15)
    fired = utc(2026, 10, 25, 0, 15)
    assert next_after(rule, BERLIN, utc(2026, 10, 25, 1, 5), fired) == utc(2026, 10, 27, 1, 15)


def test_scheduler_dst_spring_forward_gap_moves_to_the_next_valid_time() -> None:
    rule = weekly(time(2, 15), 6)  # Sunday 29 March 2026, 02:15 does not exist
    found = next_after(rule, BERLIN, utc(2026, 3, 28, 12))
    assert found == utc(2026, 3, 29, 1, 15)
    assert local(found) == "2026-03-29 03:15 CEST"
    # Set at 03:05 CEST (after the jump): still today, at 03:15.
    assert next_after(rule, BERLIN, utc(2026, 3, 29, 1, 5)) == found
    # Fired: next week at 02:15 CEST, once.
    assert upcoming(rule, BERLIN, found, 2, last_fired=found) == [
        utc(2026, 4, 5, 0, 15),
        utc(2026, 4, 12, 0, 15),
    ]


@pytest.mark.parametrize(
    ("after", "at", "day", "expected"),
    [
        # UTC midnight is 02:00 CEST on Sunday 25 October: the local date is already Sunday.
        (utc(2026, 10, 24, 23, 59), time(2, 0), 6, utc(2026, 10, 25, 0, 0)),
        (utc(2026, 10, 25, 0, 0), time(2, 1), 6, utc(2026, 10, 25, 0, 1)),
        # Local midnight (22:00 UTC in summer time) rolls the weekday.
        (utc(2026, 10, 24, 21, 59), time(0, 0), 6, utc(2026, 10, 24, 22, 0)),
        (utc(2026, 10, 24, 22, 0), time(0, 1), 6, utc(2026, 10, 24, 22, 1)),
        # Local midnight after the change is 23:00 UTC.
        (utc(2026, 10, 25, 22, 59), time(23, 59), 6, utc(2026, 10, 25, 22, 59) + timedelta(days=7)),
        (utc(2026, 10, 25, 22, 59), time(0, 0), 0, utc(2026, 10, 25, 23, 0)),
        # Spring: UTC midnight is 01:00 CET on Sunday 29 March.
        (utc(2026, 3, 28, 23, 59), time(1, 0), 6, utc(2026, 3, 29, 0, 0)),
    ],
)
def test_scheduler_dst_day_boundaries(
    after: datetime, at: time, day: int, expected: datetime
) -> None:
    assert next_after(weekly(at, day), BERLIN, after) == expected


# --- end to end: create a schedule one minute ahead, read its verdict, fire it ------------

EDGES = [
    utc(2026, 10, 24, 23, 59),  # UTC midnight, 01:59 CEST
    utc(2026, 10, 25, 0, 30),  # first 02:30 (CEST)
    utc(2026, 10, 25, 0, 58),  # 02:58 CEST, the next minute is 02:59 CEST
    utc(2026, 10, 25, 0, 59),  # 02:59 CEST, the next minute is 02:00 CET
    utc(2026, 10, 25, 1, 0),  # second 02:00 (CET)
    utc(2026, 10, 25, 1, 28),  # 02:28 CET, when CI failed
    utc(2026, 10, 25, 1, 59),  # 02:59 CET, the next minute is 03:00 CET
    utc(2026, 10, 25, 22, 59),  # local midnight after the change
    utc(2026, 3, 29, 0, 59),  # 01:59 CET, the next minute is 03:00 CEST
    utc(2026, 3, 29, 1, 0),  # 03:00 CEST
    utc(2026, 3, 28, 22, 59),  # 23:59 CET Saturday, the next minute is Sunday
]


@pytest.mark.parametrize("now", EDGES, ids=[e.strftime("%m-%dT%H:%MZ") for e in EDGES])
async def test_scheduler_dst_a_schedule_one_minute_ahead_runs_once_with_a_dry_verdict(
    now: datetime,
) -> None:
    """What the web e2e test does: the wall clock of the site plus one minute, on that day."""
    at = now + timedelta(minutes=1)
    wall = at.astimezone(BERLIN)
    async with harness() as h:
        h.clock.set(now)
        owner = await h.owner()
        mower_id = await h.register(owner)
        await h.map(mower_id, owner.account_id)
        created = await h.schedule(
            owner,
            target_type="mower",
            target_id=str(mower_id),
            days_of_week=[DAYS[wall.weekday()]],
            start_time=wall.strftime("%H:%M"),
            duration_minutes=60,
        )
        preview = (
            await h.client.get(
                f"{API}/schedules/{created['id']}/preview?count=3", headers=owner.headers
            )
        ).json()
        h.clock.set(at + timedelta(seconds=5))
        assert await h.tick() == 1
        for minutes in range(5, 24 * 60, 5):  # the rest of the day: no second firing
            h.clock.set(at + timedelta(minutes=minutes))
            assert await h.tick() == 0
        runs = await h.runs(owner, created["id"])
        after = (
            await h.client.get(f"{API}/schedules/{created['id']}", headers=owner.headers)
        ).json()
    assert created["next_run_at"] == at.strftime("%Y-%m-%dT%H:%M:%SZ")
    assert preview["items"][0]["at"] == created["next_run_at"]
    assert preview["weather"]["verdict"] == "run", preview["weather"]["reason"]
    assert [r["outcome"] for r in runs] == ["started"]
    next_run = datetime.fromisoformat(after["next_run_at"])
    assert next_run.astimezone(BERLIN).strftime("%a %H:%M") == wall.strftime("%a %H:%M")
    assert timedelta(days=6) < next_run - at < timedelta(days=8)
