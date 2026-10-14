"""Health history: blade and mowing hours, distance, battery cycles, charge sessions, errors.

Everything is derived incrementally from consecutive states of one mower (no batch recompute).
The cursor of the last accepted state lives in the running total row (:class:`HealthRow`), so
each new state only needs that row:

* **Intervals.** The interval ``[previous.observed_at, current.observed_at)`` is attributed to
  the condition of the *previous* state (the interval start). An interval longer than
  ``max_gap`` counts nothing: the mower was silent (the gateway heartbeat is 10 s), and nobody
  knows what it did meanwhile.
* **Blade hours.** When the previous state's ``raw`` carried a boolean ``blade_on`` (the
  simulator and adapters that know it), blades ran iff that flag was true. Without the flag
  blades are assumed to run while the activity is ``mowing``. The heuristic overcounts transfer
  drives of adapters without the flag (``mowing`` also covers transfers with blades off).
* **Device blade counter.** Mowers that count blade time themselves (Mammotion:
  ``report_data.maintenance.blade_used_time``, reset in the vendor app) send it as
  ``raw.blade_used_s`` and the warning threshold as ``raw.blade_used_warn_s`` (seconds). The
  newest positive values are kept on the totals row and shown instead of the own counter; a
  state without them keeps the stored values.
* **Mowing hours.** The same interval accounting with ``activity == mowing``.
* **Distance.** Haversine metres between consecutive positions. A step longer than
  ``max_step_m`` (RTK jumps) or across a gap longer than ``max_gap`` is skipped; the cursor
  still moves to the new position.
* **Battery cycles.** Armed once the battery drops below :data:`BATTERY_LOW_PERCENT`; a rise
  above :data:`BATTERY_HIGH_PERCENT` while armed counts one cycle and disarms.
* **Charge sessions.** A transition into charging (``charging`` true or activity ``charging``)
  from not charging. A mower without a previous state counts as not charging.
* **Errors.** Counted per ``error_code`` (``"unknown"`` without one) when the activity becomes
  ``error`` or the code changes while in ``error``.
* **Jobs.** A transition into ``mowing`` from anything but ``mowing`` or ``paused`` (a resume
  after a pause continues the job). The first state of a mower counts when it is ``mowing``.
* **Online time.** The interval accounting with any activity except ``offline``: a state that
  arrived means the mower was connected; a silent gap longer than ``max_gap`` counts nothing.
  Reports divide it by the length of the period (availability).

Days are UTC days: Telemetry does not know the site's time zone. An interval is attributed
wholly to the UTC day of its start; events (cycles, sessions, errors) to the day of the state
that shows them. A state whose ``observed_at`` is not after the cursor changes nothing
(redelivery or out of order), which makes the accounting idempotent.
"""

import math
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime, timedelta
from typing import Any
from uuid import UUID

from mowdeck_contracts.v1 import MowerActivity, MowerState

BATTERY_LOW_PERCENT = 20
"""A battery below this arms the cycle counter."""
BATTERY_HIGH_PERCENT = 80
"""A battery above this after the counter was armed completes one cycle."""
UNKNOWN_ERROR = "unknown"
"""Error key of an error state without ``error_code``."""
EARTH_RADIUS_M = 6_371_008.8
"""Mean earth radius (IUGG) for the haversine distance."""

_MOWING = MowerActivity.MOWING.value
_CHARGING = MowerActivity.CHARGING.value
_ERROR = MowerActivity.ERROR.value
_PAUSED = MowerActivity.PAUSED.value
_OFFLINE = MowerActivity.OFFLINE.value


@dataclass(frozen=True, slots=True)
class HealthRow:
    """Running totals and the incremental cursor of one mower (table ``mower_health``).

    ``since`` and the ``last_*`` cursor are None while no state was accepted (a blade reset
    before the first telemetry creates the row).
    """

    mower_id: UUID
    account_id: UUID
    updated_at: datetime
    since: datetime | None = None
    blade_seconds: float = 0.0
    mowing_seconds: float = 0.0
    distance_m: float = 0.0
    battery_cycles: int = 0
    charge_sessions: int = 0
    errors: dict[str, int] = field(default_factory=dict)
    blade_seconds_since_change: float = 0.0
    blade_changed_at: datetime | None = None
    blade_threshold_notified: bool = False
    last_observed_at: datetime | None = None
    last_activity: str | None = None
    last_charging: bool | None = None
    last_blade_on: bool | None = None
    last_lat: float | None = None
    last_lon: float | None = None
    last_error_code: str | None = None
    battery_armed: bool = False
    device_blade_seconds: float | None = None
    """The mower's own blade counter (``raw.blade_used_s``), None when it never sent one."""
    device_blade_warn_seconds: float | None = None
    """The mower's own blade warning threshold (``raw.blade_used_warn_s``)."""


@dataclass(frozen=True, slots=True)
class DailyRow:
    """Totals of one mower and UTC day (table ``mower_health_daily``)."""

    mower_id: UUID
    account_id: UUID
    day: date
    blade_seconds: float = 0.0
    mowing_seconds: float = 0.0
    distance_m: float = 0.0
    battery_cycles: int = 0
    charge_sessions: int = 0
    errors: dict[str, int] = field(default_factory=dict)
    jobs: int = 0
    online_seconds: float = 0.0


@dataclass(frozen=True, slots=True)
class BladeChange:
    """A recorded blade change (table ``blade_changes``)."""

    id: UUID
    mower_id: UUID
    account_id: UUID
    changed_at: datetime
    note: str | None = None
    changed_by: UUID | None = None


@dataclass(frozen=True, slots=True)
class Delta:
    """What one state adds to one UTC day."""

    day: date
    blade_seconds: float = 0.0
    mowing_seconds: float = 0.0
    distance_m: float = 0.0
    battery_cycles: int = 0
    charge_sessions: int = 0
    errors: dict[str, int] = field(default_factory=dict)
    jobs: int = 0
    online_seconds: float = 0.0


@dataclass(frozen=True, slots=True)
class HealthLimits:
    """Parameters of the accounting (from the settings)."""

    max_gap: timedelta = timedelta(seconds=120)
    max_step_m: float = 25.0
    blade_threshold_seconds: float = 50 * 3600.0


@dataclass(frozen=True, slots=True)
class HealthStep:
    """The result of applying one state: the new totals, the per-day deltas, the threshold."""

    row: HealthRow
    deltas: list[Delta]
    threshold_crossed: bool


def utc_day(at: datetime) -> date:
    """The UTC calendar day of ``at``."""
    return at.astimezone(UTC).date()


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great circle distance in metres."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * EARTH_RADIUS_M * math.asin(min(1.0, math.sqrt(a)))


def blade_flag(raw: dict[str, Any]) -> bool | None:
    """``raw.blade_on`` when the adapter reports it as a boolean, else None."""
    value = raw.get("blade_on")
    return value if isinstance(value, bool) else None


def device_seconds(raw: dict[str, Any], key: str) -> float | None:
    """A positive finite number of seconds in ``raw[key]``, else None (booleans are not)."""
    value = raw.get(key)
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    seconds = float(value)
    return seconds if math.isfinite(seconds) and seconds > 0 else None


def is_charging(state: MowerState) -> bool:
    """True while the mower charges."""
    return state.charging or state.activity.value == _CHARGING


def blades_ran(row: HealthRow) -> bool:
    """Whether blades ran after the cursor state (flag when known, else activity mowing)."""
    if row.last_blade_on is not None:
        return row.last_blade_on
    return row.last_activity == _MOWING


def merge_errors(left: dict[str, int], right: dict[str, int]) -> dict[str, int]:
    """Sum two error maps (new dict)."""
    merged = dict(left)
    for code, count in right.items():
        merged[code] = merged.get(code, 0) + count
    return merged


def advance(
    row: HealthRow | None, state: MowerState, limits: HealthLimits, now: datetime
) -> HealthStep | None:
    """Apply ``state`` (stored at ``now``) to the totals; None when it is not newer than the
    cursor."""
    current = row or HealthRow(mower_id=state.mower_id, account_id=state.account_id, updated_at=now)
    last = current.last_observed_at
    if last is not None and state.observed_at <= last:
        return None
    at = state.observed_at

    blade = mowing = distance = online = 0.0
    if last is not None and at - last <= limits.max_gap:
        seconds = (at - last).total_seconds()
        blade = seconds if blades_ran(current) else 0.0
        mowing = seconds if current.last_activity == _MOWING else 0.0
        online = seconds if current.last_activity != _OFFLINE else 0.0
        if state.position is not None and current.last_lat is not None:
            assert current.last_lon is not None  # noqa: S101 - stored together
            step = haversine_m(
                current.last_lat, current.last_lon, state.position.lat, state.position.lon
            )
            distance = step if step <= limits.max_step_m else 0.0

    armed, cycles = current.battery_armed, 0
    if state.battery_percent < BATTERY_LOW_PERCENT:
        armed = True
    elif state.battery_percent > BATTERY_HIGH_PERCENT and armed:
        armed, cycles = False, 1

    charging = is_charging(state)
    sessions = 1 if charging and not current.last_charging else 0

    jobs = (
        1
        if state.activity.value == _MOWING and current.last_activity not in (_MOWING, _PAUSED)
        else 0
    )

    errors: dict[str, int] = {}
    in_error = state.activity.value == _ERROR
    if in_error and (
        current.last_activity != _ERROR or state.error_code != current.last_error_code
    ):
        errors[state.error_code or UNKNOWN_ERROR] = 1

    since_change = current.blade_seconds_since_change + blade
    notified = current.blade_threshold_notified
    crossed = not notified and since_change >= limits.blade_threshold_seconds
    new_row = replace(
        current,
        updated_at=now,
        since=current.since or at,
        blade_seconds=current.blade_seconds + blade,
        mowing_seconds=current.mowing_seconds + mowing,
        distance_m=current.distance_m + distance,
        battery_cycles=current.battery_cycles + cycles,
        charge_sessions=current.charge_sessions + sessions,
        errors=merge_errors(current.errors, errors),
        blade_seconds_since_change=since_change,
        blade_threshold_notified=notified or crossed,
        last_observed_at=at,
        last_activity=state.activity.value,
        last_charging=charging,
        last_blade_on=blade_flag(state.raw),
        last_lat=state.position.lat if state.position else None,
        last_lon=state.position.lon if state.position else None,
        last_error_code=state.error_code if in_error else None,
        battery_armed=armed,
        device_blade_seconds=device_seconds(state.raw, "blade_used_s")
        or current.device_blade_seconds,
        device_blade_warn_seconds=device_seconds(state.raw, "blade_used_warn_s")
        or current.device_blade_warn_seconds,
    )
    events = Delta(
        day=utc_day(at), battery_cycles=cycles, charge_sessions=sessions, errors=errors, jobs=jobs
    )
    if last is None:
        deltas = [events]
    else:
        interval = Delta(
            day=utc_day(last),
            blade_seconds=blade,
            mowing_seconds=mowing,
            distance_m=distance,
            online_seconds=online,
        )
        deltas = [combine(interval, events)] if interval.day == events.day else [interval, events]
    return HealthStep(row=new_row, deltas=deltas, threshold_crossed=crossed)


def combine(left: Delta, right: Delta) -> Delta:
    """Two deltas of the same day as one."""
    return Delta(
        day=left.day,
        blade_seconds=left.blade_seconds + right.blade_seconds,
        mowing_seconds=left.mowing_seconds + right.mowing_seconds,
        distance_m=left.distance_m + right.distance_m,
        battery_cycles=left.battery_cycles + right.battery_cycles,
        charge_sessions=left.charge_sessions + right.charge_sessions,
        errors=merge_errors(left.errors, right.errors),
        jobs=left.jobs + right.jobs,
        online_seconds=left.online_seconds + right.online_seconds,
    )


def apply_delta(row: DailyRow | None, delta: Delta, mower_id: UUID, account_id: UUID) -> DailyRow:
    """Add ``delta`` to the day row (a new one when the day has none yet)."""
    base = row or DailyRow(mower_id=mower_id, account_id=account_id, day=delta.day)
    return replace(
        base,
        blade_seconds=base.blade_seconds + delta.blade_seconds,
        mowing_seconds=base.mowing_seconds + delta.mowing_seconds,
        distance_m=base.distance_m + delta.distance_m,
        battery_cycles=base.battery_cycles + delta.battery_cycles,
        charge_sessions=base.charge_sessions + delta.charge_sessions,
        errors=merge_errors(base.errors, delta.errors),
        jobs=base.jobs + delta.jobs,
        online_seconds=base.online_seconds + delta.online_seconds,
    )


def reset_blade(row: HealthRow, at: datetime) -> HealthRow:
    """The totals after a blade change at ``at``."""
    return replace(
        row,
        blade_seconds_since_change=0.0,
        blade_changed_at=at,
        blade_threshold_notified=False,
        updated_at=at,
    )
