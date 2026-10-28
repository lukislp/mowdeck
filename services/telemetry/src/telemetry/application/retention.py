"""Retention: delete expired rows and thin old positions (ADR 0017, ADR 0053).

Only one replica works at a time: the store hands out a scope under ``pg_try_advisory_lock``; a
replica that does not get it does nothing (the in-memory store has no lock and always works).

- :meth:`Retention.run_once` deletes history, positions and command timeline rows older than
  their limits (positions: 0 = keep forever), in batches.
- :meth:`Retention.thin_once` downsamples positions older than the detail window, slice by
  slice (at most 6 hours of one mower per statement), inside the nightly window only, and stops
  at its time budget. A watermark per mower makes it idempotent and cheap; new rows are never
  touched because the detail window is days long.
"""

import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Protocol
from uuid import UUID
from zoneinfo import ZoneInfo

from mowdeck_service_kit import get_logger

from telemetry.application.context import TelemetryConfig
from telemetry.application.ports import Clock, RetentionScope, RetentionTarget, Store
from telemetry.domain.thinning import floor_to_grid, in_window, slice_end

_log = get_logger("telemetry.retention")


class RetentionObserver(Protocol):
    """Receives the numbers of a round (the service exports them as metrics)."""

    def rows_deleted(self, table: str, count: int) -> None:
        """Rows removed by the age limit."""
        ...

    def rows_thinned(self, scanned: int, deleted: int) -> None:
        """Positions looked at and removed by the downsampling."""
        ...

    def round_finished(self, kind: str, seconds: float) -> None:
        """A round (``delete`` or ``thin``) ended after this many seconds."""
        ...


@dataclass(frozen=True, slots=True)
class ThinReport:
    """What one thinning round did."""

    mowers: int = 0
    slices: int = 0
    scanned: int = 0
    deleted: int = 0
    seconds: float = 0.0
    complete: bool = True
    """False when the budget or the window ended the round before all mowers were caught up."""


class Retention:
    """The periodic cleanup."""

    def __init__(
        self,
        store: Store,
        config: TelemetryConfig,
        clock: Clock,
        *,
        observer: RetentionObserver | None = None,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._store = store
        self._config = config
        self._clock = clock
        self._observer = observer
        self._monotonic = monotonic

    async def run_once(self) -> dict[RetentionTarget, int] | None:
        """Delete expired rows; None when another replica holds the lock."""
        limits = {
            RetentionTarget.HISTORY: self._config.retention_history,
            RetentionTarget.POSITIONS: self._config.retention_positions,
            RetentionTarget.COMMANDS: self._config.retention_commands,
        }
        began = self._monotonic()
        async with self._store.retention() as scope:
            if scope is None:
                _log.debug("retention skipped, another replica holds the lock")
                return None
            now = self._clock()
            deleted: dict[RetentionTarget, int] = {}
            for target, limit in limits.items():
                if limit <= timedelta(0):
                    continue  # keep forever
                deleted[target] = await scope.delete_before(
                    target, now - limit, self._config.retention_batch_size
                )
        if self._observer is not None:
            for target, count in deleted.items():
                self._observer.rows_deleted(target.value, count)
            self._observer.round_finished("delete", self._monotonic() - began)
        return deleted

    def window_open(self) -> bool:
        """Whether the local time is inside the nightly maintenance window."""
        config = self._config
        return in_window(
            self._clock(),
            ZoneInfo(config.thinning_zone),
            config.thinning_window_start_hour,
            config.thinning_window_end_hour,
        )

    async def thin_once(self) -> ThinReport | None:
        """Downsample old positions; None when disabled, outside the window or locked out."""
        if not self._config.thinning_enabled or not self.window_open():
            return None
        began = self._monotonic()
        async with self._store.retention() as scope:
            if scope is None:
                _log.debug("thinning skipped, another replica holds the lock")
                return None
            report = await self._thin(scope, began)
        if self._observer is not None:
            self._observer.rows_thinned(report.scanned, report.deleted)
            self._observer.round_finished("thin", report.seconds)
        return report

    async def _thin(self, scope: RetentionScope, began: float) -> ThinReport:
        limit = floor_to_grid(self._clock() - self._config.positions_detail)
        mowers = slices = scanned = deleted = 0
        complete = True
        for mower_id in await scope.thin_candidates():
            start = await scope.thin_start(mower_id)
            if start is None or start >= limit:
                continue
            mowers += 1
            done = await self._thin_mower(scope, mower_id, floor_to_grid(start), limit, began)
            slices += done[0]
            scanned += done[1]
            deleted += done[2]
            if not done[3]:
                complete = False
                break
        return ThinReport(
            mowers, slices, scanned, deleted, self._monotonic() - began, complete=complete
        )

    async def _thin_mower(
        self, scope: RetentionScope, mower_id: UUID, first: datetime, limit: datetime, began: float
    ) -> tuple[int, int, int, bool]:
        """Slices of one mower; the last item says whether it caught up with ``limit``."""
        config = self._config
        start = first
        slices = scanned = deleted = 0
        while start < limit:
            over_budget = self._monotonic() - began >= config.thinning_budget.total_seconds()
            if over_budget or not self.window_open():  # out of time, or the night is over
                return slices, scanned, deleted, False
            end = slice_end(start, limit)
            result = await scope.thin_slice(mower_id, start, end, config.thin_policy)
            slices += 1
            scanned += result.scanned
            deleted += result.deleted
            start = end
        return slices, scanned, deleted, True
