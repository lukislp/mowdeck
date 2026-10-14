"""Rules of the Scheduler without I/O: validation, the busy rule, pattern rotation."""

import json
import math
import zoneinfo
from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta
from typing import Any, Final

from scheduler.domain.errors import InvalidInput
from scheduler.domain.models import Schedule

CAN_VIEW: Final = "can_view"
CAN_CONTROL: Final = "can_control"
CAN_EDIT_SCHEDULE: Final = "can_edit_schedule"

BUSY_ACTIVITIES: Final = frozenset({"mowing", "paused", "returning"})
"""A mower in one of these activities is not started again (``skipped_mower_busy``)."""
RUNNING_ACTIVITIES: Final = frozenset({"mowing", "paused"})
"""A started job in one of these activities may be sent back to the dock."""
FINISHED_ACTIVITIES: Final = frozenset({"docked", "charging", "idle"})
"""After one of these (seen a while after the start) the job of a run is over."""

WEEKDAYS: Final = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
"""Day names of the API, index = ``date.weekday()``."""
MAX_PATTERN_BYTES: Final = 4096


def validate_timezone(name: str) -> str:
    """An IANA time zone name; ``InvalidInput`` otherwise."""
    try:
        zoneinfo.ZoneInfo(name)
    except (zoneinfo.ZoneInfoNotFoundError, ValueError):
        msg = f"unknown time zone {name!r}"
        raise InvalidInput(msg) from None
    return name


def _check_recurrence(schedule: Schedule) -> None:
    if schedule.kind == "weekly":
        if not schedule.days_of_week or not set(schedule.days_of_week) <= set(range(7)):
            raise InvalidInput("a weekly schedule needs at least one day of the week")
        schedule.days_of_week = sorted(set(schedule.days_of_week))
        schedule.anchor_date = None
        schedule.interval_days = None
    elif schedule.kind == "every_n_days":
        if schedule.anchor_date is None or not schedule.interval_days:
            raise InvalidInput("an every_n_days schedule needs anchor_date and interval_days")
        if not 1 <= schedule.interval_days <= 365:
            raise InvalidInput("interval_days must be between 1 and 365")
        schedule.days_of_week = []
    else:
        raise InvalidInput("kind must be weekly or every_n_days")


def _check_rain(schedule: Schedule) -> None:
    rain = schedule.rain_settings
    if not 0 <= rain.probability_threshold <= 100:
        raise InvalidInput("rain_pause.probability_threshold must be between 0 and 100")
    if not 0 <= rain.precipitation_mm_threshold <= 100:
        raise InvalidInput("rain_pause.precipitation_mm_threshold must be between 0 and 100")
    if not 0 <= rain.lookahead_hours <= 24 or not 0 <= rain.cooldown_hours <= 24:
        raise InvalidInput("rain_pause hours must be between 0 and 24")


def validate_schedule(schedule: Schedule) -> None:
    """Consistency of a schedule (create and update share it); normalises in place."""
    name = schedule.name.strip()
    if not 1 <= len(name) <= 100:
        raise InvalidInput("name must have 1 to 100 characters")
    schedule.name = name
    if schedule.target_type not in ("mower", "group"):
        raise InvalidInput("target_type must be mower or group")
    _check_recurrence(schedule)
    if not 1 <= schedule.duration_minutes <= 24 * 60:
        raise InvalidInput("duration_minutes must be between 1 and 1440")
    if schedule.target_type == "group" and schedule.area_ids:
        raise InvalidInput("a group schedule mows all areas of each mower (no area_ids)")
    if len(schedule.area_ids) > 50 or any(not 1 <= len(a) <= 128 for a in schedule.area_ids):
        raise InvalidInput("area_ids: at most 50 ids of 1 to 128 characters")
    if len(schedule.rotation_angles) > 24 or any(
        not math.isfinite(a) or not -360 <= a <= 360 for a in schedule.rotation_angles
    ):
        raise InvalidInput("rotation_angles: at most 24 angles between -360 and 360")
    if schedule.pattern is not None and len(json.dumps(schedule.pattern)) > MAX_PATTERN_BYTES:
        raise InvalidInput(f"pattern must not exceed {MAX_PATTERN_BYTES} bytes")
    if schedule.timezone_override is not None:
        validate_timezone(schedule.timezone_override)
    _check_rain(schedule)


def pattern_for_run(
    pattern: Mapping[str, Any] | None, rotation: Sequence[float], index: int
) -> dict[str, Any] | None:
    """The pattern settings of the next run: the base pattern with ``angle`` taken round robin
    from ``rotation`` (the plan's "rotate between patterns", e.g. a new direction every week)."""
    if not rotation:
        return dict(pattern) if pattern is not None else None
    result = dict(pattern or {})
    result["angle"] = rotation[index % len(rotation)]
    return result


def window_end(occurrence: datetime, duration_minutes: int) -> datetime:
    """When the window of an occurrence closes."""
    return occurrence + timedelta(minutes=duration_minutes)
