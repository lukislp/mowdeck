"""Reads: latest state, history, position track and command timeline.

Every query filters by the account of the verified principal (the third isolation layer behind
the OpenFGA check of the route). Time ranges and row counts are bounded.
"""

import asyncio
from collections.abc import Collection
from contextlib import aclosing
from dataclasses import dataclass
from datetime import datetime, timedelta
from uuid import UUID

from mowdeck_contracts.v1 import MAX_TRACK_POINTS, MowerCapabilities, MowerMap

from telemetry.application.context import TelemetryConfig
from telemetry.application.heatmap import (
    CELL_SIZES,
    DEFAULT_CUTTING_WIDTH_MM,
    Heatmap,
    HeatmapBuilder,
    Layer,
)
from telemetry.application.ports import Clock, Store
from telemetry.domain.errors import InvalidQueryError, NoSnapshotYetError, NoTelemetryYetError
from telemetry.domain.models import (
    CommandEvent,
    HistoryPoint,
    LatestRow,
    PositionRow,
    SnapshotRow,
)
from telemetry.domain.rules import bin_count, link_lost, link_seen_at, parse_step
from telemetry.domain.thinning import thinned_join_seconds


@dataclass(frozen=True, slots=True)
class HistoryResult:
    """Points of a history query and whether more existed than ``query_max_rows``."""

    start: datetime
    end: datetime
    step: timedelta | None
    points: list[HistoryPoint]
    truncated: bool


@dataclass(frozen=True, slots=True)
class PositionResult:
    """Positions of a query, oldest first."""

    start: datetime
    end: datetime
    positions: list[PositionRow]
    truncated: bool


@dataclass(frozen=True, slots=True)
class HeatmapResult:
    """A heatmap layer of one mower and the frame it was computed in."""

    start: datetime
    end: datetime
    mower_map: MowerMap
    heatmap: Heatmap
    truncated: bool


HEATMAP_CHUNK_ROWS = 5000
"""Rows per chunk streamed from the store into the heatmap builder."""


class Queries:
    """Read use cases."""

    def __init__(self, store: Store, config: TelemetryConfig, clock: Clock) -> None:
        self._store = store
        self._config = config
        self._clock = clock

    def is_stale(self, row: LatestRow) -> bool:
        """True when the mower is offline (ADR 0040): the gateway reported the link lost, or its
        last heartbeat (``observed_at`` for states without one) is older than the threshold.
        Old values of a connected mower are not stale."""
        if link_lost(row):
            return True
        return self._clock() - link_seen_at(row) > self._config.stale_after

    async def state(self, account_id: UUID, mower_id: UUID) -> LatestRow:
        """The latest state; raises :class:`NoTelemetryYetError` when none was received."""
        row = await self._store.latest_of(account_id, mower_id)
        if row is None:
            msg = "no telemetry yet"
            raise NoTelemetryYetError(msg)
        return row

    async def states(
        self, account_id: UUID, mower_ids: Collection[UUID] | None
    ) -> tuple[list[LatestRow], bool]:
        """Latest states of the account's mowers (``None``: all of them) and a truncation flag."""
        limit = self._config.states_limit
        rows = await self._store.latest_many(account_id, mower_ids, limit + 1)
        return rows[:limit], len(rows) > limit

    async def capabilities(
        self, account_id: UUID, mower_ids: Collection[UUID] | None
    ) -> tuple[list[SnapshotRow], bool]:
        """Stored capabilities of the account's mowers (``None``: all) and a truncation flag."""
        limit = self._config.states_limit
        rows = await self._store.snapshots_of_kind(account_id, mower_ids, "capabilities", limit + 1)
        return rows[:limit], len(rows) > limit

    def _range(
        self,
        start: datetime | None,
        end: datetime | None,
        step: timedelta | None = None,
        default: timedelta | None = None,
    ) -> tuple[datetime, datetime]:
        stop = end or self._clock()
        default = default or self._config.default_range
        if step is not None:  # the widest default range the bins allow
            default = min(default, step * (self._config.query_max_rows - 1))
        first = start or stop - default
        if first >= stop:
            msg = "'from' must be before 'to'"
            raise InvalidQueryError(msg)
        if stop - first > self._config.query_max_range:
            days = self._config.query_max_range.days
            msg = f"the time range may span at most {days} days"
            raise InvalidQueryError(msg)
        return first, stop

    async def history(
        self,
        account_id: UUID,
        mower_id: UUID,
        start: datetime | None,
        end: datetime | None,
        step: str | None,
    ) -> HistoryResult:
        """History of a mower; binned with ``date_bin`` when ``step`` is given."""
        width: timedelta | None = None
        if step is not None:
            try:
                width = parse_step(step)
            except ValueError as exc:
                raise InvalidQueryError(str(exc)) from exc
        first, stop = self._range(start, end, width)
        if width is not None and bin_count(first, stop, width) > self._config.query_max_rows:
            rows = self._config.query_max_rows
            msg = f"step is too small: the range would need more than {rows} bins"
            raise InvalidQueryError(msg)
        limit = self._config.query_max_rows
        points = await self._store.history(account_id, mower_id, first, stop, width, limit + 1)
        return HistoryResult(first, stop, width, points[:limit], len(points) > limit)

    async def positions(
        self,
        account_id: UUID,
        mower_id: UUID,
        start: datetime | None,
        end: datetime | None,
        limit: int,
    ) -> PositionResult:
        """The newest positions in the range (at most ``limit``), oldest first."""
        first, stop = self._range(start, end)
        bounded = max(1, min(limit, self._config.query_max_rows))
        rows = await self._store.positions(account_id, mower_id, first, stop, bounded + 1)
        return PositionResult(first, stop, rows[-bounded:], len(rows) > bounded)

    async def track(
        self, account_id: UUID, mower_id: UUID, start: datetime, end: datetime, limit: int
    ) -> PositionResult:
        """The position track for coverage (``tracks.positions.query``): like
        :meth:`positions` but bounded by ``MAX_TRACK_POINTS`` instead of ``query_max_rows``."""
        first, stop = self._range(start, end)
        bounded = max(1, min(limit, MAX_TRACK_POINTS))
        rows = await self._store.positions(account_id, mower_id, first, stop, bounded + 1)
        return PositionResult(first, stop, rows[-bounded:], len(rows) > bounded)

    async def commands(self, account_id: UUID, mower_id: UUID, limit: int) -> list[CommandEvent]:
        """The newest command transitions, newest first."""
        bounded = max(1, min(limit, self._config.query_max_rows))
        return await self._store.commands(account_id, mower_id, bounded)

    async def snapshot(self, account_id: UUID, mower_id: UUID, kind: str) -> SnapshotRow:
        """The stored map or capabilities; raises :class:`NoSnapshotYetError` without one."""
        row = await self._store.snapshot_of(account_id, mower_id, kind)
        if row is None:
            msg = f"no {kind} received from the mower yet"
            raise NoSnapshotYetError(msg, kind)
        return row

    async def mow_path(self, account_id: UUID, mower_id: UUID) -> SnapshotRow:
        """The mow path of the running job; raises :class:`NoSnapshotYetError` (kind ``path``)
        without one or once the gateway cleared it (the job ended)."""
        row = await self._store.snapshot_of(account_id, mower_id, "path")
        if row is None or not row.body:
            msg = "the mower has no planned path right now"
            raise NoSnapshotYetError(msg, "path")
        return row

    async def heatmap(
        self,
        account_id: UUID,
        mower_id: UUID,
        layer: Layer,
        start: datetime | None,
        end: datetime | None,
        cell_m: float,
    ) -> HeatmapResult:
        """A heatmap layer over the mower's map frame (default range: the last 7 days).

        Raises :class:`NoSnapshotYetError` (map) without a map, :class:`InvalidQueryError` for
        a bad range or cell size. Reads at most ``heatmap_max_rows`` positions, newest first.
        """
        if cell_m not in CELL_SIZES:
            sizes = ", ".join(f"{size:g}" for size in CELL_SIZES)
            msg = f"cell must be one of {sizes}"
            raise InvalidQueryError(msg)
        first, stop = self._range(start, end, default=self._config.heatmap_default_range)
        map_row = await self.snapshot(account_id, mower_id, "map")
        mower_map = MowerMap.model_validate(map_row.body)
        caps_row = await self._store.snapshot_of(account_id, mower_id, "capabilities")
        width_mm = DEFAULT_CUTTING_WIDTH_MM
        if caps_row is not None:
            width_mm = MowerCapabilities.model_validate(caps_row.body).cutting_width_mm or width_mm
        builder = HeatmapBuilder(
            layer,
            mower_map.reference,
            width_mm / 1000,
            join_seconds=self._config.heatmap_join_seconds,
            join_m=self._config.heatmap_join_m,
            thinned_before=(
                self._clock() - self._config.positions_detail
                if self._config.thinning_enabled
                else None
            ),
            thin_join_seconds=thinned_join_seconds(
                self._config.thin_policy, self._config.heatmap_join_seconds
            ),
        )
        limit = self._config.heatmap_max_rows
        scanned = 0
        truncated = False
        chunks = self._store.position_chunks(
            account_id, mower_id, first, stop, limit + 1, HEATMAP_CHUNK_ROWS
        )
        async with aclosing(chunks):
            async for chunk in chunks:
                take = chunk[: limit - scanned]
                builder.add_all(take)
                scanned += len(take)
                if len(take) < len(chunk):
                    truncated = True
                    break
        # Shapely releases the GIL; the event loop keeps serving while the layer is computed.
        heatmap = await asyncio.to_thread(builder.build, cell_m, mower_map)
        return HeatmapResult(first, stop, mower_map, heatmap, truncated)
