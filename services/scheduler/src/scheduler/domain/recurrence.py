"""When a schedule fires: weekly on chosen days, or every N days from an anchor date, always at
a wall-clock time in the schedule's time zone.

Occurrences are computed per local calendar date and only then converted to UTC with
:mod:`zoneinfo` (``fold=0``), which gives the two DST rules we want without special cases:

* a time that does not exist (spring forward, e.g. 02:30 on the last Sunday of March in
  Europe/Berlin) uses the offset before the change, so it fires one hour later in wall-clock
  time (03:30 CEST), on the same day;
* a time that exists twice (fall back, 02:30 on the last Sunday of October) fires once, at the
  first occurrence (02:30 CEST).

"Every N days" counts calendar days in the local zone, so the wall-clock time stays the same
across DST changes (the interval in hours is 23 or 25 on those days). See ADR 0024.
"""

from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from typing import Literal
from zoneinfo import ZoneInfo

RecurrenceKind = Literal["weekly", "every_n_days"]

MAX_SEARCH_DAYS = 800
"""Upper bound of the search for the next occurrence (every N days allows N up to 365)."""


@dataclass(frozen=True, slots=True)
class Recurrence:
    """A recurrence rule. ``days`` are ISO weekdays as Python ``date.weekday()`` (Monday 0)."""

    kind: RecurrenceKind
    at: time
    days: frozenset[int] = frozenset()
    anchor: date | None = None
    interval_days: int | None = None

    def matches(self, day: date) -> bool:
        """Does the rule fire on the local calendar date ``day``?"""
        if self.kind == "weekly":
            return day.weekday() in self.days
        if self.anchor is None or not self.interval_days:
            return False
        delta = (day - self.anchor).days
        return delta >= 0 and delta % self.interval_days == 0


def local_to_utc(day: date, at: time, zone: ZoneInfo) -> datetime:
    """The UTC instant of wall-clock ``at`` on ``day`` in ``zone`` (``fold=0``, see module)."""
    return datetime.combine(day, at.replace(tzinfo=None, fold=0), tzinfo=zone).astimezone(UTC)


def occurrences(rule: Recurrence, zone: ZoneInfo, after: datetime) -> Iterator[datetime]:
    """UTC instants of the rule strictly after ``after``, in order (lazy, may be endless)."""
    day = after.astimezone(zone).date() - timedelta(days=1)  # a DST gap can move into "after"
    last = day + timedelta(days=MAX_SEARCH_DAYS)
    previous: datetime | None = None
    while day <= last:
        if rule.matches(day):
            instant = local_to_utc(day, rule.at, zone)
            if instant > after and (previous is None or instant > previous):
                previous = instant
                yield instant
        day += timedelta(days=1)


def next_after(rule: Recurrence, zone: ZoneInfo, after: datetime) -> datetime | None:
    """The first occurrence strictly after ``after``, or None when the rule never fires."""
    return next(occurrences(rule, zone, after), None)


def upcoming(rule: Recurrence, zone: ZoneInfo, after: datetime, count: int) -> list[datetime]:
    """The next ``count`` occurrences after ``after``."""
    found: list[datetime] = []
    for instant in occurrences(rule, zone, after):
        found.append(instant)
        if len(found) >= count:
            break
    return found
