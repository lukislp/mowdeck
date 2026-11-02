"""In-memory store (tests and local experiments) with the semantics of the PostgreSQL store.

Transactions run one at a time and are all or nothing (the snapshot is restored on error).
Downsampling uses the pure helpers of ``telemetry.domain.rules``; the retention scope has no
lock and is always granted.
"""

import asyncio
from collections.abc import AsyncGenerator, AsyncIterator, Collection
from contextlib import asynccontextmanager
from dataclasses import replace as replace_fields
from datetime import date, datetime, timedelta
from typing import Protocol
from uuid import UUID

from telemetry.application.ports import RetentionScope, RetentionTarget, ThinResult, Tx
from telemetry.domain.health import BladeChange, DailyRow, HealthRow
from telemetry.domain.models import (
    CommandEvent,
    DirectoryEntry,
    HistoryPoint,
    HistoryRow,
    LatestRow,
    PositionRow,
    SnapshotRow,
)
from telemetry.domain.rules import downsample, raw_points
from telemetry.domain.thinning import CONTEXT, ThinPolicy, keep_set


class _Owned(Protocol):
    """A row that carries its account."""

    @property
    def account_id(self) -> UUID: ...


class _Data:
    """The rows; copied for the rollback snapshot."""

    def __init__(self) -> None:
        self.latest: dict[UUID, LatestRow] = {}
        self.history: dict[tuple[UUID, datetime], HistoryRow] = {}
        self.positions: dict[tuple[UUID, datetime], PositionRow] = {}
        self.thinned_until: dict[UUID, datetime] = {}
        self.commands: dict[tuple[UUID, str], CommandEvent] = {}
        self.directory: dict[UUID, DirectoryEntry] = {}
        self.snapshots: dict[tuple[UUID, str], SnapshotRow] = {}
        self.health: dict[UUID, HealthRow] = {}
        self.daily: dict[tuple[UUID, date], DailyRow] = {}
        self.blade_changes: dict[UUID, BladeChange] = {}

    def snapshot(self) -> "_Data":
        """A copy to restore on rollback (rows are immutable, so the dicts suffice)."""
        other = _Data()
        other.latest = dict(self.latest)
        other.history = dict(self.history)
        other.positions = dict(self.positions)
        other.thinned_until = dict(self.thinned_until)
        other.commands = dict(self.commands)
        other.directory = dict(self.directory)
        other.snapshots = dict(self.snapshots)
        other.health = dict(self.health)
        other.daily = dict(self.daily)
        other.blade_changes = dict(self.blade_changes)
        return other


class _MemoryTx:
    def __init__(self, data: _Data) -> None:
        self._data = data

    async def latest(self, mower_id: UUID) -> LatestRow | None:
        return self._data.latest.get(mower_id)

    async def put_latest(self, row: LatestRow) -> None:
        existing = self._data.latest.get(row.mower_id)
        if existing is None or existing.observed_at <= row.observed_at:
            self._data.latest[row.mower_id] = row

    async def add_history(self, row: HistoryRow) -> None:
        self._data.history.setdefault((row.mower_id, row.observed_at), row)

    async def add_position(self, row: PositionRow) -> None:
        self._data.positions.setdefault((row.mower_id, row.observed_at), row)

    async def add_command_event(self, event: CommandEvent) -> None:
        self._data.commands.setdefault((event.command_id, event.status), event)

    async def put_snapshot(self, row: SnapshotRow) -> None:
        key = (row.mower_id, row.kind)
        existing = self._data.snapshots.get(key)
        if existing is None or existing.observed_at <= row.observed_at:
            self._data.snapshots[key] = row

    async def directory_account(self, mower_id: UUID) -> UUID | None:
        entry = self._data.directory.get(mower_id)
        return entry.account_id if entry else None

    async def put_directory(self, entry: DirectoryEntry, *, replace: bool) -> None:
        existing = self._data.directory.get(entry.mower_id)
        if replace or existing is None:
            self._data.directory[entry.mower_id] = entry
        elif existing.name is None or existing.serial is None:
            self._data.directory[entry.mower_id] = replace_fields(
                existing, name=existing.name or entry.name, serial=existing.serial or entry.serial
            )

    async def drop_mower(self, mower_id: UUID) -> None:
        data = self._data
        data.directory.pop(mower_id, None)
        data.latest.pop(mower_id, None)
        data.history = {k: v for k, v in data.history.items() if k[0] != mower_id}
        data.positions = {k: v for k, v in data.positions.items() if k[0] != mower_id}
        data.thinned_until.pop(mower_id, None)
        data.commands = {k: v for k, v in data.commands.items() if v.mower_id != mower_id}
        data.snapshots = {k: v for k, v in data.snapshots.items() if k[0] != mower_id}
        data.health.pop(mower_id, None)
        data.daily = {k: v for k, v in data.daily.items() if k[0] != mower_id}
        data.blade_changes = {k: v for k, v in data.blade_changes.items() if v.mower_id != mower_id}

    async def health(self, mower_id: UUID) -> HealthRow | None:
        return self._data.health.get(mower_id)

    async def put_health(self, row: HealthRow) -> None:
        self._data.health[row.mower_id] = row

    async def daily(self, mower_id: UUID, day: date) -> DailyRow | None:
        return self._data.daily.get((mower_id, day))

    async def put_daily(self, row: DailyRow) -> None:
        self._data.daily[(row.mower_id, row.day)] = row

    async def add_blade_change(self, change: BladeChange) -> None:
        self._data.blade_changes.setdefault(change.id, change)


class _MemoryRetention:
    def __init__(self, data: _Data) -> None:
        self._data = data

    async def delete_before(
        self, target: RetentionTarget, cutoff: datetime, batch_size: int
    ) -> int:
        del batch_size
        data = self._data
        before: int
        match target:
            case RetentionTarget.HISTORY:
                before = len(data.history)
                data.history = {k: v for k, v in data.history.items() if v.observed_at >= cutoff}
                return before - len(data.history)
            case RetentionTarget.POSITIONS:
                before = len(data.positions)
                data.positions = {
                    k: v for k, v in data.positions.items() if v.observed_at >= cutoff
                }
                return before - len(data.positions)
            case RetentionTarget.COMMANDS:
                before = len(data.commands)
                data.commands = {k: v for k, v in data.commands.items() if v.at >= cutoff}
                return before - len(data.commands)

    async def thin_candidates(self) -> list[UUID]:
        return list(self._data.directory)

    async def thin_start(self, mower_id: UUID) -> datetime | None:
        stored = self._data.thinned_until.get(mower_id)
        if stored is not None:
            return stored
        times = [k[1] for k in self._data.positions if k[0] == mower_id]
        return min(times) if times else None

    async def thin_slice(
        self, mower_id: UUID, start: datetime, end: datetime, policy: ThinPolicy
    ) -> ThinResult:
        data = self._data
        rows = sorted(
            (
                row
                for (owner, at), row in data.positions.items()
                if owner == mower_id and start - CONTEXT <= at < end + CONTEXT
            ),
            key=lambda row: row.observed_at,
        )
        errors = {
            at for (owner, at), h in data.history.items() if owner == mower_id and h.error_code
        }
        kept = keep_set(rows, start, end, policy, errors)
        inside = [row.observed_at for row in rows if start <= row.observed_at < end]
        doomed = [at for at in inside if at not in kept]
        for at in doomed:
            del data.positions[(mower_id, at)]
        current = data.thinned_until.get(mower_id)
        if current is None or current < end:
            data.thinned_until[mower_id] = end
        return ThinResult(scanned=len(inside), deleted=len(doomed))


class MemoryStore:
    """The :class:`~telemetry.application.ports.Store` port in memory."""

    def __init__(self) -> None:
        self.data = _Data()
        self._lock = asyncio.Lock()
        self.lock_held_elsewhere = False
        """Tests set this to simulate another replica holding the retention lock."""

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[Tx]:
        async with self._lock:
            snapshot = self.data.snapshot()
            try:
                yield _MemoryTx(self.data)
            except BaseException:
                self.data = snapshot
                raise

    async def directory_of(self, mower_id: UUID) -> DirectoryEntry | None:
        return self.data.directory.get(mower_id)

    async def latest_of(self, account_id: UUID, mower_id: UUID) -> LatestRow | None:
        row = self.data.latest.get(mower_id)
        return row if row is not None and row.account_id == account_id else None

    async def latest_many(
        self, account_id: UUID, mower_ids: Collection[UUID] | None, limit: int
    ) -> list[LatestRow]:
        rows = [
            r
            for r in self.data.latest.values()
            if r.account_id == account_id and (mower_ids is None or r.mower_id in mower_ids)
        ]
        rows.sort(key=lambda r: str(r.mower_id))
        return rows[:limit]

    async def history(
        self,
        account_id: UUID,
        mower_id: UUID,
        start: datetime,
        end: datetime,
        step: timedelta | None,
        limit: int,
    ) -> list[HistoryPoint]:
        rows = sorted(
            (
                r
                for r in self.data.history.values()
                if r.account_id == account_id
                and r.mower_id == mower_id
                and start <= r.observed_at < end
            ),
            key=lambda r: r.observed_at,
        )
        if step is None:
            return raw_points(rows)[:limit]
        positions = [
            p
            for p in self.data.positions.values()
            if p.account_id == account_id
            and p.mower_id == mower_id
            and start <= p.observed_at < end
        ]
        return downsample(rows, positions, step)[:limit]

    async def positions(
        self, account_id: UUID, mower_id: UUID, start: datetime, end: datetime, limit: int
    ) -> list[PositionRow]:
        rows = sorted(
            (
                p
                for p in self.data.positions.values()
                if p.account_id == account_id
                and p.mower_id == mower_id
                and start <= p.observed_at < end
            ),
            key=lambda p: p.observed_at,
        )
        return rows[-limit:]

    async def position_chunks(
        self,
        account_id: UUID,
        mower_id: UUID,
        start: datetime,
        end: datetime,
        limit: int,
        chunk_size: int,
    ) -> AsyncGenerator[list[PositionRow]]:
        rows = sorted(
            (
                p
                for p in self.data.positions.values()
                if p.account_id == account_id
                and p.mower_id == mower_id
                and start <= p.observed_at < end
            ),
            key=lambda p: p.observed_at,
            reverse=True,
        )[:limit]
        for offset in range(0, len(rows), chunk_size):
            yield rows[offset : offset + chunk_size]

    async def commands(self, account_id: UUID, mower_id: UUID, limit: int) -> list[CommandEvent]:
        rows = [
            c
            for c in self.data.commands.values()
            if c.account_id == account_id and c.mower_id == mower_id
        ]
        rows.sort(key=lambda c: (c.at, str(c.command_id)), reverse=True)
        return rows[:limit]

    async def snapshot_of(self, account_id: UUID, mower_id: UUID, kind: str) -> SnapshotRow | None:
        row = self.data.snapshots.get((mower_id, kind))
        return row if row is not None and row.account_id == account_id else None

    async def snapshots_of_kind(
        self, account_id: UUID, mower_ids: Collection[UUID] | None, kind: str, limit: int
    ) -> list[SnapshotRow]:
        rows = [
            r
            for (_, k), r in self.data.snapshots.items()
            if k == kind
            and r.account_id == account_id
            and (mower_ids is None or r.mower_id in mower_ids)
        ]
        rows.sort(key=lambda r: str(r.mower_id))
        return rows[:limit]

    async def health_of(self, account_id: UUID, mower_id: UUID) -> HealthRow | None:
        row = self.data.health.get(mower_id)
        return row if row is not None and row.account_id == account_id else None

    async def daily_of(
        self, account_id: UUID, mower_id: UUID, first: date, last: date
    ) -> list[DailyRow]:
        rows = [
            r
            for r in self.data.daily.values()
            if r.account_id == account_id and r.mower_id == mower_id and first <= r.day <= last
        ]
        return sorted(rows, key=lambda r: r.day)

    async def daily_many(
        self,
        account_id: UUID,
        mower_ids: Collection[UUID] | None,
        first: date,
        last: date,
        limit: int,
    ) -> list[DailyRow]:
        rows = [
            r
            for r in self.data.daily.values()
            if r.account_id == account_id
            and (mower_ids is None or r.mower_id in mower_ids)
            and first <= r.day <= last
        ]
        rows.sort(key=lambda r: (str(r.mower_id), r.day))
        return rows[:limit]

    async def blade_changes(
        self, account_id: UUID, mower_id: UUID, limit: int
    ) -> list[BladeChange]:
        rows = [
            c
            for c in self.data.blade_changes.values()
            if c.account_id == account_id and c.mower_id == mower_id
        ]
        rows.sort(key=lambda c: (c.changed_at, str(c.id)), reverse=True)
        return rows[:limit]

    @asynccontextmanager
    async def retention(self) -> AsyncIterator[RetentionScope | None]:
        yield None if self.lock_held_elsewhere else _MemoryRetention(self.data)

    async def purge_account(self, account_id: UUID) -> int:
        async with self._lock:
            data = self.data
            mine = {
                *(e.mower_id for e in data.directory.values() if e.account_id == account_id),
                *(r.mower_id for r in data.latest.values() if r.account_id == account_id),
                *(p.mower_id for p in data.positions.values() if p.account_id == account_id),
            }
            others = {e.mower_id for e in data.directory.values() if e.account_id != account_id}
            removed = 0
            before = len(data.thinned_until)
            data.thinned_until = {
                k: v for k, v in data.thinned_until.items() if k not in mine or k in others
            }
            removed += before - len(data.thinned_until)

            def drop[K, V: _Owned](rows: dict[K, V]) -> dict[K, V]:
                nonlocal removed
                kept = {k: v for k, v in rows.items() if v.account_id != account_id}
                removed += len(rows) - len(kept)
                return kept

            data.directory = drop(data.directory)
            data.latest = drop(data.latest)
            data.history = drop(data.history)
            data.positions = drop(data.positions)
            data.commands = drop(data.commands)
            data.snapshots = drop(data.snapshots)
            data.health = drop(data.health)
            data.daily = drop(data.daily)
            data.blade_changes = drop(data.blade_changes)
            return removed
