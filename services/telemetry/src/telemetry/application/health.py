"""Health history use cases: totals, UTC day totals, battery series and blade changes.

The totals are written by :class:`~telemetry.application.ingest.Ingest` while states arrive
(see :mod:`telemetry.domain.health`). Every read filters by the account of the verified
principal. A blade reset before the first state creates the totals row, so the change is not
lost; such a row has ``since`` None until telemetry arrives. Day totals are kept (they are small,
one row per mower and UTC day); the retention job does not trim them.
"""

from collections.abc import Collection
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from uuid import UUID, uuid4

from telemetry.application.context import TelemetryConfig
from telemetry.application.ports import Clock, Store
from telemetry.application.queries import HistoryResult, Queries
from telemetry.domain.errors import InvalidQueryError, NoTelemetryYetError
from telemetry.domain.health import BladeChange, DailyRow, HealthRow, reset_blade, utc_day

DAILY_DEFAULT_DAYS = 30
"""Days returned by the daily query when no range is given (ending today, UTC)."""
DAILY_MAX_DAYS = 366
"""Longest range of the daily query (inclusive days)."""
BLADE_CHANGES_LIMIT = 100
"""Most blade changes one query returns."""
REPORT_MAX_ROWS = 50_000
"""Most day rows one report returns (e.g. 1 300 mowers over 31 days); ``truncated`` beyond."""


@dataclass(frozen=True, slots=True)
class DailyResult:
    """Day totals in ``[first, last]``, oldest first (days without data are absent)."""

    first: date
    last: date
    days: list[DailyRow]


@dataclass(frozen=True, slots=True)
class ReportResult:
    """Day rows of several mowers in ``[first, last]``, by mower and day."""

    first: date
    last: date
    rows: list[DailyRow]
    truncated: bool


class Health:
    """Health history reads and the blade reset."""

    def __init__(
        self, store: Store, config: TelemetryConfig, clock: Clock, queries: Queries
    ) -> None:
        self._store = store
        self._config = config
        self._clock = clock
        self._queries = queries

    @property
    def threshold_hours(self) -> float:
        """The configured blade hours threshold."""
        return self._config.blade_hours_threshold

    async def totals(self, account_id: UUID, mower_id: UUID) -> HealthRow:
        """The running totals; raises :class:`NoTelemetryYetError` when nothing was recorded."""
        row = await self._store.health_of(account_id, mower_id)
        if row is None:
            msg = "no telemetry yet"
            raise NoTelemetryYetError(msg)
        return row

    async def daily(
        self, account_id: UUID, mower_id: UUID, first: date | None, last: date | None
    ) -> DailyResult:
        """Day totals of an inclusive range (default: the last 30 UTC days)."""
        start, stop = self._days(first, last)
        rows = await self._store.daily_of(account_id, mower_id, start, stop)
        return DailyResult(start, stop, rows)

    def _days(self, first: date | None, last: date | None) -> tuple[date, date]:
        stop = last or utc_day(self._clock())
        start = first or stop - timedelta(days=DAILY_DEFAULT_DAYS - 1)
        if start > stop:
            msg = "'from' must not be after 'to'"
            raise InvalidQueryError(msg)
        if (stop - start).days + 1 > DAILY_MAX_DAYS:
            msg = f"the date range may span at most {DAILY_MAX_DAYS} days"
            raise InvalidQueryError(msg)
        return start, stop

    async def report(
        self,
        account_id: UUID,
        mower_ids: Collection[UUID] | None,
        first: date | None,
        last: date | None,
    ) -> ReportResult:
        """Day totals of the given mowers (``None``: every mower of the account) in one query;
        same range rules as :meth:`daily`."""
        start, stop = self._days(first, last)
        rows = await self._store.daily_many(account_id, mower_ids, start, stop, REPORT_MAX_ROWS + 1)
        return ReportResult(start, stop, rows[:REPORT_MAX_ROWS], len(rows) > REPORT_MAX_ROWS)

    async def battery(
        self,
        account_id: UUID,
        mower_id: UUID,
        start: datetime | None,
        end: datetime | None,
        step: str | None,
    ) -> HistoryResult:
        """The battery series: the history query (same ranges, bins and limits)."""
        return await self._queries.history(account_id, mower_id, start, end, step)

    async def reset_blade(
        self, account_id: UUID, mower_id: UUID, changed_by: UUID | None, note: str | None
    ) -> HealthRow:
        """Record a blade change and restart the blade hours since the change."""
        now = self._clock()
        async with self._store.transaction() as tx:
            current = await tx.health(mower_id)
            if current is not None and current.account_id != account_id:
                msg = "no telemetry yet"
                raise NoTelemetryYetError(msg)
            base = current or HealthRow(mower_id=mower_id, account_id=account_id, updated_at=now)
            row = reset_blade(base, now)
            await tx.put_health(row)
            await tx.add_blade_change(
                BladeChange(
                    id=uuid4(),
                    mower_id=mower_id,
                    account_id=account_id,
                    changed_at=now,
                    note=note,
                    changed_by=changed_by,
                )
            )
        return row

    async def blade_changes(self, account_id: UUID, mower_id: UUID) -> list[BladeChange]:
        """The newest blade changes, newest first."""
        return await self._store.blade_changes(account_id, mower_id, BLADE_CHANGES_LIMIT)
