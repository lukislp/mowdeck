"""Ports: what the use cases need from the outside world, as protocols."""

from collections.abc import AsyncGenerator, Collection
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from enum import StrEnum
from typing import Protocol
from uuid import UUID

from mowdeck_contracts.v1 import BladeHoursThreshold

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
from telemetry.domain.thinning import ThinPolicy


class Clock(Protocol):
    """Returns the current time (timezone-aware)."""

    def __call__(self) -> datetime:
        """The current time."""
        ...


class HealthEvents(Protocol):
    """Publishes the events of the health history (after the commit)."""

    async def blade_threshold(self, event: BladeHoursThreshold) -> None:
        """Announce that the blade hours since the last change crossed the threshold."""
        ...


@dataclass(frozen=True, slots=True)
class ThinResult:
    """What thinning one slice did."""

    scanned: int
    deleted: int


class RetentionTarget(StrEnum):
    """The tables the retention job trims."""

    HISTORY = "mower_history"
    POSITIONS = "mower_positions"
    COMMANDS = "command_timeline"


class Tx(Protocol):
    """Writes of one transaction (ingestion)."""

    async def latest(self, mower_id: UUID) -> LatestRow | None:
        """The latest row of a mower, locked against concurrent writers."""
        ...

    async def put_latest(self, row: LatestRow) -> None:
        """Insert or replace the latest row; never replaces a newer ``observed_at``."""
        ...

    async def add_history(self, row: HistoryRow) -> None:
        """Append a history row (a repeated ``(mower_id, observed_at)`` is ignored)."""
        ...

    async def add_position(self, row: PositionRow) -> None:
        """Append a position (a repeated ``(mower_id, observed_at)`` is ignored)."""
        ...

    async def add_command_event(self, event: CommandEvent) -> None:
        """Append a command transition (a repeated ``(command_id, status)`` is ignored)."""
        ...

    async def put_snapshot(self, row: SnapshotRow) -> None:
        """Insert or replace a snapshot; never replaces a newer ``observed_at``."""
        ...

    async def directory_account(self, mower_id: UUID) -> UUID | None:
        """The account that owns a mower, None when unknown."""
        ...

    async def put_directory(self, entry: DirectoryEntry, *, replace: bool) -> None:
        """Insert a directory entry; with ``replace`` also overwrite an existing one."""
        ...

    async def drop_mower(self, mower_id: UUID) -> None:
        """Delete the directory entry and every row of the mower."""
        ...

    async def health(self, mower_id: UUID) -> HealthRow | None:
        """The health totals of a mower, locked against concurrent writers."""
        ...

    async def put_health(self, row: HealthRow) -> None:
        """Insert or replace the health totals (the caller holds the row lock)."""
        ...

    async def daily(self, mower_id: UUID, day: date) -> DailyRow | None:
        """The health totals of one mower and UTC day."""
        ...

    async def put_daily(self, row: DailyRow) -> None:
        """Insert or replace the totals of one mower and UTC day."""
        ...

    async def add_blade_change(self, change: BladeChange) -> None:
        """Record a blade change."""
        ...


class RetentionScope(Protocol):
    """Held by the one replica that may delete (see :meth:`Store.retention`)."""

    async def delete_before(
        self, target: RetentionTarget, cutoff: datetime, batch_size: int
    ) -> int:
        """Delete rows older than ``cutoff`` in batches; returns the number of rows."""
        ...

    async def thin_candidates(self) -> list[UUID]:
        """The mowers whose track may need thinning (the directory)."""
        ...

    async def thin_start(self, mower_id: UUID) -> datetime | None:
        """Where thinning of this mower's track resumes: the stored watermark, else the time of
        its oldest position; None without positions."""
        ...

    async def thin_slice(
        self, mower_id: UUID, start: datetime, end: datetime, policy: ThinPolicy
    ) -> ThinResult:
        """Thin ``[start, end)`` of one mower's track (rows the policy does not keep are
        deleted) and move the watermark to ``end``. Idempotent; ``start`` and ``end`` are on
        the 5 minute grid."""
        ...


class Store(Protocol):
    """Persistence of telemetry."""

    def transaction(self) -> AbstractAsyncContextManager[Tx]:
        """A write transaction (all or nothing)."""
        ...

    async def directory_of(self, mower_id: UUID) -> DirectoryEntry | None:
        """The directory entry of a mower (account, name, serial), None when unknown."""
        ...

    async def latest_of(self, account_id: UUID, mower_id: UUID) -> LatestRow | None:
        """The latest row of one mower of an account."""
        ...

    async def latest_many(
        self, account_id: UUID, mower_ids: Collection[UUID] | None, limit: int
    ) -> list[LatestRow]:
        """Latest rows of an account's mowers (all, or only ``mower_ids``), at most ``limit``."""
        ...

    async def history(
        self,
        account_id: UUID,
        mower_id: UUID,
        start: datetime,
        end: datetime,
        step: timedelta | None,
        limit: int,
    ) -> list[HistoryPoint]:
        """History in ``[start, end)``, oldest first, at most ``limit`` points.

        With ``step`` the rows are binned (``date_bin``), without it every row is returned.
        """
        ...

    async def positions(
        self, account_id: UUID, mower_id: UUID, start: datetime, end: datetime, limit: int
    ) -> list[PositionRow]:
        """The newest ``limit`` positions in ``[start, end)``, oldest first."""
        ...

    def position_chunks(
        self,
        account_id: UUID,
        mower_id: UUID,
        start: datetime,
        end: datetime,
        limit: int,
        chunk_size: int,
    ) -> AsyncGenerator[list[PositionRow]]:
        """Positions in ``[start, end)``, newest first, at most ``limit`` rows, in lists of at
        most ``chunk_size`` (streamed, for the heatmaps)."""
        ...

    async def commands(self, account_id: UUID, mower_id: UUID, limit: int) -> list[CommandEvent]:
        """The newest ``limit`` command transitions, newest first."""
        ...

    async def snapshot_of(self, account_id: UUID, mower_id: UUID, kind: str) -> SnapshotRow | None:
        """The stored snapshot of one kind of one mower of an account."""
        ...

    async def snapshots_of_kind(
        self, account_id: UUID, mower_ids: Collection[UUID] | None, kind: str, limit: int
    ) -> list[SnapshotRow]:
        """Snapshots of one kind of an account's mowers (all, or only ``mower_ids``), at most
        ``limit``, ordered by mower."""
        ...

    async def health_of(self, account_id: UUID, mower_id: UUID) -> HealthRow | None:
        """The health totals of one mower of an account."""
        ...

    async def daily_of(
        self, account_id: UUID, mower_id: UUID, first: date, last: date
    ) -> list[DailyRow]:
        """Day rows in ``[first, last]`` (inclusive), oldest first."""
        ...

    async def daily_many(
        self,
        account_id: UUID,
        mower_ids: Collection[UUID] | None,
        first: date,
        last: date,
        limit: int,
    ) -> list[DailyRow]:
        """Day rows of several mowers (``None``: all of the account) in ``[first, last]``,
        ordered by mower and day, at most ``limit``."""
        ...

    async def blade_changes(
        self, account_id: UUID, mower_id: UUID, limit: int
    ) -> list[BladeChange]:
        """The newest ``limit`` blade changes, newest first."""
        ...

    def retention(self) -> AbstractAsyncContextManager[RetentionScope | None]:
        """Try to become the replica that deletes; yields None when another one is."""
        ...

    async def purge_account(self, account_id: UUID) -> int:
        """Delete every row of an account in one transaction (demo sandbox purge, ADR 0076):
        directory, latest states, history, positions, command timeline, snapshots, health
        totals, day rows, blade changes and the thinning watermarks of its mowers. Returns the
        number of rows removed (0 when nothing was left)."""
        ...
