"""Downsampling policy of the position track (ADR 0053).

Pure functions. The PostgreSQL store runs the same rules as one SQL statement per slice
(``SqlStore`` / ``_SqlRetention.thin_slice``); a test pins both to the same kept set.

A row older than the full detail window is kept when it is

- the first row of its bin (``active_seconds`` wide while the blades run or the mower moves,
  ``idle_seconds`` wide otherwise; bins are anchored at 2000-01-01 UTC like the history bins), or
- an edge: the first or last row before a gap of more than ``gap_seconds``, or a row next to a
  change of ``blades_on`` or of the RTK fix (start and stop of a job, docking, lost fix), or a
  bend of the track by more than 25 degrees (lane ends and corners keep their shape), or
- a row that was observed at the same instant as a history row with an error code.

Everything else is dropped. The result still contains every job boundary, so the coverage and
signal heatmaps keep their shape; consecutive kept rows of one swath are at most two active
bins apart (:func:`thinned_join_seconds`).
"""

import math
from collections.abc import Collection, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from telemetry.domain.models import PositionRow

BIN_ORIGIN_EPOCH_US = int(datetime(2000, 1, 1, tzinfo=UTC).timestamp()) * 1_000_000
"""The bin origin of ``date_bin`` in microseconds since the epoch."""

MOVING_METRES = 1.0
"""A row farther than this from the previous one counts as moving (without blades)."""

METRES_PER_DEGREE = 111_320.0
"""Planar approximation, good enough for a lawn (the same constant is used in the SQL)."""

TURN_DEGREES = 25.0
"""A row where the track bends by more than this is kept: the corners of the swaths survive."""

TURN_COS = math.cos(math.radians(TURN_DEGREES))
"""The same limit as a cosine (also passed to the SQL)."""

TURN_MIN_STEP_M = 0.3
"""Steps shorter than this do not count for a bend (RTK jitter, a mower standing still)."""

CONTEXT = timedelta(minutes=10)
"""Rows this far outside a slice are read to see the neighbours of its edge rows."""

SLICE = timedelta(hours=6)
"""Width of one thinning slice; slices are aligned to the UTC day."""

GRID_SECONDS = 300
"""Slice ends are aligned to this grid, so bins never straddle a slice edge (5 min bins)."""


@dataclass(frozen=True, slots=True)
class ThinPolicy:
    """Which rows of an old track survive."""

    active_seconds: int = 30
    """One row per bin of this width while the blades run or the mower moves."""
    idle_seconds: int = 300
    """One row per bin of this width while it stands (docked, paused)."""
    gap_seconds: int = 120
    """A gap longer than this makes the rows next to it edges that are always kept."""

    def __post_init__(self) -> None:
        if self.active_seconds < 1 or self.idle_seconds < self.active_seconds:
            msg = "active_seconds must be positive and idle_seconds at least as large"
            raise ValueError(msg)
        if GRID_SECONDS % self.active_seconds or GRID_SECONDS % self.idle_seconds:
            msg = f"bin widths must divide {GRID_SECONDS} seconds"
            raise ValueError(msg)


def thinned_join_seconds(policy: ThinPolicy, join_seconds: float) -> float:
    """The join limit of the coverage swaths for thinned rows: two active bins plus margin."""
    return max(join_seconds, 2 * policy.active_seconds + 5.0)


def floor_to_grid(at: datetime) -> datetime:
    """``at`` rounded down to the 5 minute grid (UTC)."""
    epoch = int(at.timestamp())
    return datetime.fromtimestamp(epoch - epoch % GRID_SECONDS, UTC)


def slice_end(start: datetime, limit: datetime) -> datetime:
    """The end of the slice starting at ``start``: the next 6 hour boundary, at most ``limit``."""
    epoch = int(start.timestamp())
    size = int(SLICE.total_seconds())
    return min(datetime.fromtimestamp(epoch - epoch % size + size, UTC), limit)


def _bin(at: datetime, width: int) -> int:
    micros = (at - datetime(1970, 1, 1, tzinfo=UTC)) // timedelta(microseconds=1)
    return (micros - BIN_ORIGIN_EPOCH_US) // (width * 1_000_000)


def _moved(row: PositionRow, previous: PositionRow | None) -> bool:
    if previous is None:
        return False
    dy = (row.lat - previous.lat) * METRES_PER_DEGREE
    dx = (row.lon - previous.lon) * METRES_PER_DEGREE * math.cos(math.radians(row.lat))
    return math.hypot(dx, dy) > MOVING_METRES


def _turns(row: PositionRow, previous: PositionRow | None, following: PositionRow | None) -> bool:
    """Whether the track bends by more than :data:`TURN_DEGREES` at ``row`` (steps shorter than
    :data:`TURN_MIN_STEP_M` are RTK noise and never count)."""
    if previous is None or following is None:
        return False
    scale = METRES_PER_DEGREE * math.cos(math.radians(row.lat))
    dx1, dy1 = (row.lon - previous.lon) * scale, (row.lat - previous.lat) * METRES_PER_DEGREE
    dx2, dy2 = (following.lon - row.lon) * scale, (following.lat - row.lat) * METRES_PER_DEGREE
    n1, n2 = math.hypot(dx1, dy1), math.hypot(dx2, dy2)
    if n1 < TURN_MIN_STEP_M or n2 < TURN_MIN_STEP_M:
        return False
    return dx1 * dx2 + dy1 * dy2 < TURN_COS * n1 * n2


def keep_set(
    rows: Sequence[PositionRow],
    start: datetime,
    end: datetime,
    policy: ThinPolicy,
    error_times: Collection[datetime] = (),
) -> set[datetime]:
    """The ``observed_at`` of the rows in ``[start, end)`` that survive.

    ``rows`` are the rows of ONE mower in ``[start - CONTEXT, end + CONTEXT)`` in time order;
    the context rows only provide neighbours and are never part of the result.
    """
    gap = timedelta(seconds=policy.gap_seconds)
    errors = set(error_times)
    kept: set[datetime] = set()
    seen: set[tuple[bool, int]] = set()
    for i, row in enumerate(rows):
        previous = rows[i - 1] if i > 0 else None
        following = rows[i + 1] if i + 1 < len(rows) else None
        active = row.blades_on is True or _moved(row, previous)
        width = policy.active_seconds if active else policy.idle_seconds
        key = (active, _bin(row.observed_at, width))
        first = key not in seen
        seen.add(key)
        if not start <= row.observed_at < end:
            continue
        edge = (
            previous is None
            or following is None
            or row.observed_at - previous.observed_at > gap
            or following.observed_at - row.observed_at > gap
            or row.blades_on != previous.blades_on
            or row.blades_on != following.blades_on
            or row.rtk_fix != previous.rtk_fix
            or row.rtk_fix != following.rtk_fix
            or row.observed_at in errors
            or _turns(row, previous, following)
        )
        if first or edge:
            kept.add(row.observed_at)
    return kept


def in_window(now: datetime, zone: ZoneInfo, start_hour: int, end_hour: int) -> bool:
    """Whether the local wall clock hour of ``now`` lies in ``[start_hour, end_hour)``.

    The comparison uses the local hour of the aware datetime, so a night with a DST change
    (25 or 23 hours) needs no special case: on the repeated hour of the autumn change the window
    simply opens twice, in the skipped hour of spring it has nothing to open. A window that
    wraps midnight (22 to 5) works; ``start_hour == end_hour`` means always.
    """
    if start_hour == end_hour:
        return True
    hour = now.astimezone(zone).hour
    if start_hour < end_hour:
        return start_hour <= hour < end_hour
    return hour >= start_hour or hour < end_hour
