"""Rules of the Scheduler without I/O: validation, the busy rule, the settings of a run."""

import json
import math
import zoneinfo
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Final

from mowdeck_contracts.v1 import (
    MowerCapabilities,
    MowPattern,
    SchedulePattern,
    SetBladeHeight,
    SetSpeed,
)
from pydantic import ValidationError

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
BLADE_HEIGHT_RANGE_MM: Final = (10, 120)
"""The contract's limits of ``set_blade_height`` (the mower's own range is narrower)."""
MAX_SPEED_MPS: Final = 2.0
"""The contract's limit of ``set_speed``."""
JOB_OPTION_KEYS: Final = ("obstacle_detection", "obstacle_laps", "order", "start_progress_pct")
"""``MowPattern`` keys a mower must declare in ``start_options``."""


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


def schedule_pattern(raw: Mapping[str, Any] | None) -> dict[str, Any] | None:
    """The stored form of a schedule's pattern: the ``SchedulePattern`` keys it sets, None
    when it sets none. ``InvalidInput`` for a value out of the contract's range (the API
    refuses unknown keys and ``start_progress_pct`` before)."""
    if not raw:
        return None
    try:
        keys = SchedulePattern.model_validate(dict(raw)).keys()
    except ValidationError as exc:
        problems = "; ".join(
            f"{'.'.join(str(part) for part in error['loc'])}: {error['msg']}"
            for error in exc.errors()
        )
        raise InvalidInput(f"pattern: {problems}") from None
    return keys or None


def _check_setpoints(schedule: Schedule) -> None:
    blade = schedule.blade_height_mm
    low, high = BLADE_HEIGHT_RANGE_MM
    if blade is not None and not low <= blade <= high:
        raise InvalidInput(f"blade_height_mm must be between {low} and {high}")
    speed = schedule.speed_mps
    if speed is not None and not (math.isfinite(speed) and 0 < speed <= MAX_SPEED_MPS):
        raise InvalidInput(f"speed_mps must be above 0 and at most {MAX_SPEED_MPS:g}")


def sets_mower_options(schedule: Schedule) -> bool:
    """Whether the schedule sets anything a mower has to declare: the pattern name, the
    spacing, a job option, the blade height or the speed (angle and border laps need no
    declaration)."""
    pattern = schedule.pattern or {}
    declared = ("name", "spacing_m", *JOB_OPTION_KEYS)
    return (
        any(pattern.get(key) is not None for key in declared)
        or schedule.blade_height_mm is not None
        or schedule.speed_mps is not None
    )


def mower_problem(schedule: Schedule, capabilities: MowerCapabilities | None) -> str | None:
    """Why the start settings of ``schedule`` do not fit one mower; None when they do.

    A mower whose capabilities are not known yet (its gateway never reported them) offers
    nothing: the schedule may set only the angle and the border laps for it.
    """
    if not sets_mower_options(schedule):
        return None
    if capabilities is None:
        return "its capabilities are not known yet"
    problem = SchedulePattern.model_validate(schedule.pattern or {}).problem(capabilities)
    if problem is not None:
        return problem
    blade = schedule.blade_height_mm
    if blade is not None and not capabilities.blade_height_allowed(blade):
        if capabilities.blade_height_range_mm is None:
            return "the blade height is not settable on this mower"
        low, high = capabilities.blade_height_range_mm
        return f"blade_height_mm must be between {low} and {high} on this mower"
    speed = schedule.speed_mps
    if speed is not None and not capabilities.speed_allowed(speed):
        if capabilities.max_speed_mps is None:
            return "the speed is not settable on this mower"
        low_speed = capabilities.min_speed_mps or 0
        high_speed = capabilities.max_speed_mps
        return f"speed_mps must be between {low_speed:g} and {high_speed:g} on this mower"
    return None


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
    schedule.pattern = schedule_pattern(schedule.pattern)
    _check_setpoints(schedule)
    if schedule.timezone_override is not None:
        validate_timezone(schedule.timezone_override)
    _check_rain(schedule)


RUN_PATTERN_BASE: Mapping[str, Any] = {"name": "stripes", "angle_deg": 0.0, "border_laps": 1}
"""A scheduled start without saved mower defaults: stripes with one border lap (the edges get
mowed), like the web start sheet's fallback (``MowPattern`` defaults)."""


def pattern_for_run(
    pattern: Mapping[str, Any] | None,
    rotation: Sequence[float],
    index: int,
    defaults: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """The ``StartMow.pattern`` of the next run, shaped like ``MowPattern``.

    Precedence, lowest first: :data:`RUN_PATTERN_BASE`, the mower's saved mowing defaults
    (``TargetMower.mow_pattern``), the keys the schedule sets (``SchedulePattern``), then the
    angle taken round robin from ``rotation`` (the plan's "rotate between patterns", e.g. a new
    direction every week). A key nobody sets is left out, so the mower keeps its own setting.
    Angles are normalised to 0 up to 180 degrees (stripes have no direction). A start progress
    in the defaults is never carried into a scheduled run (it belongs to one manual start).
    """
    result: dict[str, Any] = {**RUN_PATTERN_BASE, **(defaults or {})}
    result.update({k: v for k, v in (pattern or {}).items() if v is not None})
    result.pop("start_progress_pct", None)
    if rotation:
        result["angle_deg"] = rotation[index % len(rotation)]
    angle = result.get("angle_deg")
    if isinstance(angle, int | float) and math.isfinite(angle):
        result["angle_deg"] = float(angle) % 180.0
    return {k: v for k, v in result.items() if v is not None}


@dataclass(slots=True)
class RunSettings:
    """What one scheduled start sends to one mower: the setpoints first, then ``StartMow``
    with ``pattern``. ``dropped`` names what was left out because the mower does not offer it
    (recorded in the run's details)."""

    pattern: dict[str, Any]
    setpoints: list[SetBladeHeight | SetSpeed] = field(default_factory=list)
    dropped: list[str] = field(default_factory=list)


def run_settings(
    schedule: Schedule,
    defaults: Mapping[str, Any] | None,
    capabilities: MowerCapabilities | None,
) -> RunSettings:
    """The settings of the next run of ``schedule`` for one mower: schedule over mow defaults
    over the mower's own setting (:func:`pattern_for_run`), fitted to the mower.

    The schedule was checked against every mower of its target when it was saved, but a
    mower may have changed since (firmware) or joined the group later, and the mow defaults
    are not checked at all. So with known capabilities every job option, spacing, blade height
    or speed the mower does not offer is dropped (the mower keeps its own setting) instead of
    failing the start; the pattern name is left for the controller to refuse. Without known
    capabilities everything is sent as set and the controller decides.
    """
    pattern = pattern_for_run(
        schedule.pattern, schedule.rotation_angles, schedule.rotation_index, defaults
    )
    result = RunSettings(pattern=pattern)
    caps = capabilities
    if caps is not None:
        for key in JOB_OPTION_KEYS:
            value = pattern.get(key)
            if value is None:
                continue
            try:
                option = MowPattern.model_validate({key: value})
            except ValidationError:
                continue  # malformed: left for the controller to refuse
            if caps.start_options.problem(option) is not None:
                del pattern[key]
                result.dropped.append(key)
        spacing = pattern.get("spacing_m")
        if isinstance(spacing, int | float) and (
            caps.path_spacing_range_mm is None
            or not caps.path_spacing_range_mm[0]
            <= round(spacing * 1000)
            <= caps.path_spacing_range_mm[1]
        ):
            del pattern["spacing_m"]
            result.dropped.append("spacing_m")
    blade = schedule.blade_height_mm
    if blade is not None:
        if caps is None or caps.blade_height_allowed(blade):
            result.setpoints.append(SetBladeHeight(mm=blade))
        else:
            result.dropped.append("blade_height_mm")
    speed = schedule.speed_mps
    if speed is not None:
        if caps is None or caps.speed_allowed(speed):
            result.setpoints.append(SetSpeed(mps=speed))
        else:
            result.dropped.append("speed_mps")
    return result


def window_end(occurrence: datetime, duration_minutes: int) -> datetime:
    """When the window of an occurrence closes."""
    return occurrence + timedelta(minutes=duration_minutes)
