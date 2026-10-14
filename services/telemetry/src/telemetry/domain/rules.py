"""Pure rules: what a state message changes, and the arithmetic of downsampling bins.

Write rules per state message (design section 2.1, with the corrections of ADR 0017):

1. The latest row is replaced unless the message is older than the stored one.
2. History gets a row when the significant fields changed, or the last history row is at least
   ``heartbeat_seconds`` older. The Device-Gateway republishes an unchanged state every 10 s as
   a heartbeat; storing each one would multiply the table for no information, while the
   periodic row keeps gaps (a mower that went silent) visible.
3. The position track gets a row when a position is present and it (or the RTK fix) differs from
   the last stored one.

Bins use the same origin as ``date_bin(step, observed_at, '2000-01-01'::timestamptz)`` in
PostgreSQL, so the memory store and the SQL store agree.
"""

import math
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from mowdeck_contracts.v1 import MowerState

from telemetry.domain.health import blade_flag
from telemetry.domain.models import HistoryPoint, HistoryRow, LatestRow, PositionRow

BIN_ORIGIN = datetime(2000, 1, 1, tzinfo=UTC)
"""Origin of all bins (the ``date_bin`` origin)."""
MIN_STEP = timedelta(seconds=1)

_STEP = re.compile(r"^(\d{1,6})(s|m|h|d)$")
_UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400}


@dataclass(frozen=True, slots=True)
class Decision:
    """What one state message writes."""

    latest: bool
    history: bool
    position: bool

    @property
    def nothing(self) -> bool:
        """True when the message changes nothing."""
        return not (self.latest or self.history or self.position)


def state_fingerprint(state: MowerState) -> str:
    """The significant fields of a state, as one comparable string."""
    parts = (
        state.activity.value,
        state.battery_percent,
        state.charging,
        state.rtk_fix.value,
        state.rtk_source.value,
        state.connection.value,
        state.blade_height_mm,
        state.speed_mps,
        state.error_code,
    )
    return "|".join("" if p is None else str(p) for p in parts)


SIGNAL_BUCKET_DBM = 5
"""Signal strengths are compared in steps of this many dBm for the position rule."""


def _signal_bucket(raw: dict[str, Any], key: str) -> str:
    dbm = signal_dbm(raw, key)
    return "" if dbm is None else str(dbm // SIGNAL_BUCKET_DBM)


def position_fingerprint(state: MowerState) -> str | None:
    """Position (about a centimetre), RTK fix and the signal buckets, or None without a position.

    The signal buckets (5 dBm steps of WLAN and mobile) make a standing mower, typically the
    one in its dock, write a position row when its reception changes, so the signal heatmap
    gets samples at the dock too.
    """
    if state.position is None:
        return None
    return (
        f"{state.position.lat:.7f},{state.position.lon:.7f},{state.rtk_fix.value},"
        f"{_signal_bucket(state.raw, 'wifi_rssi')},{_signal_bucket(state.raw, 'mnet_rssi')}"
    )


def decide(state: MowerState, latest: LatestRow | None, *, heartbeat_seconds: float) -> Decision:
    """Apply the write rules to ``state`` given the stored latest row."""
    has_position = state.position is not None
    if latest is None:
        return Decision(latest=True, history=True, position=has_position)
    if state.observed_at < latest.observed_at:
        return Decision(latest=False, history=False, position=False)
    due = (
        latest.history_written_at is None
        or latest.history_fingerprint != state_fingerprint(state)
        or state.observed_at - latest.history_written_at >= timedelta(seconds=heartbeat_seconds)
    )
    heartbeat_due = (
        latest.history_written_at is None
        or state.observed_at - latest.history_written_at >= timedelta(seconds=heartbeat_seconds)
    )
    # A standing mower still writes a position row with each heartbeat (and when its signal
    # bucket changes, see position_fingerprint), so the signal layers get dock samples.
    moved = has_position and (
        position_fingerprint(state) != latest.position_fingerprint or heartbeat_due
    )
    return Decision(latest=True, history=due, position=moved)


def history_row(state: MowerState) -> HistoryRow:
    """The history row of a state."""
    return HistoryRow(
        account_id=state.account_id,
        mower_id=state.mower_id,
        observed_at=state.observed_at,
        activity=state.activity.value,
        battery_percent=state.battery_percent,
        charging=state.charging,
        rtk_fix=state.rtk_fix.value,
        connection=state.connection.value,
        blade_height_mm=state.blade_height_mm,
        speed_mps=state.speed_mps,
        error_code=state.error_code,
    )


def position_row(state: MowerState) -> PositionRow | None:
    """The position row of a state, None without a position."""
    if state.position is None:
        return None
    return PositionRow(
        account_id=state.account_id,
        mower_id=state.mower_id,
        observed_at=state.observed_at,
        lat=state.position.lat,
        lon=state.position.lon,
        alt=state.position.alt,
        heading=state.position.heading,
        rtk_fix=state.rtk_fix.value,
        rtk_source=state.rtk_source.value,
        wifi_rssi=signal_dbm(state.raw, "wifi_rssi"),
        mnet_rssi=signal_dbm(state.raw, "mnet_rssi"),
        blades_on=blades_on(state),
    )


MIN_DBM = -150
"""Weakest plausible signal; lower values and 0 or above are "no reading"."""


def signal_dbm(raw: dict[str, Any], key: str) -> int | None:
    """A signal strength in dBm from ``raw[key]``: an integer in ``[-150, -1]``, else None.

    Adapters report 0 (or nothing) while the link is down; booleans are not numbers.
    """
    value = raw.get(key)
    if isinstance(value, bool) or not isinstance(value, int | float) or not math.isfinite(value):
        return None
    dbm = round(value)
    return dbm if MIN_DBM <= dbm < 0 else None


def blades_on(state: MowerState) -> bool:
    """Whether the blades run: ``raw.blade_on`` when reported, else activity ``mowing``."""
    flag = blade_flag(state.raw)
    return flag if flag is not None else state.activity.value == "mowing"


# --- downsampling -----------------------------------------------------------------------------


def parse_step(text: str) -> timedelta:
    """Parse ``5s``, ``1m``, ``2h`` or ``1d`` into a timedelta (at least one second)."""
    match = _STEP.match(text.strip().lower())
    if match is None:
        msg = "step must look like 5s, 1m, 2h or 1d"
        raise ValueError(msg)
    step = timedelta(seconds=int(match.group(1)) * _UNITS[match.group(2)])
    if step < MIN_STEP:
        msg = "step must be at least 1s"
        raise ValueError(msg)
    return step


def bin_start(at: datetime, step: timedelta) -> datetime:
    """Start of the bin that contains ``at`` (same arithmetic as ``date_bin``)."""
    return BIN_ORIGIN + ((at - BIN_ORIGIN) // step) * step


def bin_edges(start: datetime, end: datetime, step: timedelta) -> list[datetime]:
    """Starts of every bin that overlaps ``[start, end)``, including empty ones."""
    edges: list[datetime] = []
    edge = bin_start(start, step)
    while edge < end:
        edges.append(edge)
        edge += step
    return edges


def bin_count(start: datetime, end: datetime, step: timedelta) -> int:
    """How many bins ``[start, end)`` spans."""
    if end <= start:
        return 0
    return (bin_start(end - timedelta(microseconds=1), step) - bin_start(start, step)) // step + 1


def _round_half_up(value: float) -> int:
    return math.floor(value + 0.5)


def raw_points(rows: Iterable[HistoryRow]) -> list[HistoryPoint]:
    """History rows as points, one sample each."""
    return [
        HistoryPoint(
            at=r.observed_at,
            activity=r.activity,
            battery_percent=r.battery_percent,
            battery_min=r.battery_percent,
            battery_max=r.battery_percent,
            charging=r.charging,
            rtk_fix=r.rtk_fix,
            error_code=r.error_code,
            samples=1,
        )
        for r in rows
    ]


def downsample(
    rows: Sequence[HistoryRow], positions: Sequence[PositionRow], step: timedelta
) -> list[HistoryPoint]:
    """Bin history rows (and the last position of each bin), oldest bin first.

    Per bin: the last activity, RTK fix and error code, the rounded mean and the extremes of
    the battery, charging if any sample was charging, and the last known position.
    Empty bins are left out. This is the pure twin of the ``date_bin`` query.
    """
    bins: dict[datetime, list[HistoryRow]] = {}
    for row in sorted(rows, key=lambda r: r.observed_at):
        bins.setdefault(bin_start(row.observed_at, step), []).append(row)
    last_position: dict[datetime, PositionRow] = {}
    for pos in sorted(positions, key=lambda p: p.observed_at):
        last_position[bin_start(pos.observed_at, step)] = pos
    points: list[HistoryPoint] = []
    for at, members in sorted(bins.items()):
        levels = [m.battery_percent for m in members]
        errors = [m.error_code for m in members if m.error_code is not None]
        position = last_position.get(at)
        points.append(
            HistoryPoint(
                at=at,
                activity=members[-1].activity,
                battery_percent=_round_half_up(sum(levels) / len(levels)),
                battery_min=min(levels),
                battery_max=max(levels),
                charging=any(m.charging for m in members),
                rtk_fix=members[-1].rtk_fix,
                error_code=errors[-1] if errors else None,
                samples=len(members),
                lat=position.lat if position else None,
                lon=position.lon if position else None,
            )
        )
    return points
