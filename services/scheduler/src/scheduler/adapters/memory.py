"""In-memory store (tests and local experiments). Same semantics as the PostgreSQL store:
transactions are all or nothing (a snapshot is restored on error) and run one at a time, so a
claimed schedule is never claimed twice."""

import asyncio
import copy
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime
from uuid import UUID

from mowdeck_contracts.v1 import MowerCapabilities

from scheduler.domain.models import MowerPlace, MowerStatus, Schedule, ScheduleRun
from scheduler.domain.rotation import AreaAngle, MowerRotation


@dataclass
class MemoryData:
    """All rows of the memory store."""

    schedules: dict[UUID, Schedule] = field(default_factory=dict)
    runs: dict[UUID, ScheduleRun] = field(default_factory=dict)
    status: dict[UUID, MowerStatus] = field(default_factory=dict)
    places: dict[UUID, MowerPlace] = field(default_factory=dict)
    capabilities: dict[UUID, tuple[datetime, MowerCapabilities]] = field(default_factory=dict)
    angles: dict[tuple[UUID, str], AreaAngle] = field(default_factory=dict)
    rotation: dict[UUID, MowerRotation] = field(default_factory=dict)


class MemoryTx:
    """``Tx`` over the memory data."""

    def __init__(self, data: MemoryData) -> None:
        self._data = data

    async def add_schedule(self, schedule: Schedule) -> None:
        self._data.schedules[schedule.id] = copy.deepcopy(schedule)

    async def get_schedule(
        self, schedule_id: UUID, account_id: UUID | None = None, *, for_update: bool = False
    ) -> Schedule | None:
        del for_update  # transactions are serialised
        found = self._data.schedules.get(schedule_id)
        if found is None or (account_id is not None and found.account_id != account_id):
            return None
        return copy.deepcopy(found)

    async def list_schedules(self, account_id: UUID) -> list[Schedule]:
        rows = [s for s in self._data.schedules.values() if s.account_id == account_id]
        return [copy.deepcopy(s) for s in sorted(rows, key=lambda s: (s.name, str(s.id)))]

    async def count_schedules(self, account_id: UUID) -> int:
        return sum(1 for s in self._data.schedules.values() if s.account_id == account_id)

    async def update_schedule(self, schedule: Schedule) -> None:
        if schedule.id in self._data.schedules:
            self._data.schedules[schedule.id] = copy.deepcopy(schedule)

    async def delete_schedule(self, schedule_id: UUID, account_id: UUID) -> bool:
        found = self._data.schedules.get(schedule_id)
        if found is None or found.account_id != account_id:
            return False
        del self._data.schedules[schedule_id]
        for run_id in [r.id for r in self._data.runs.values() if r.schedule_id == schedule_id]:
            del self._data.runs[run_id]
        return True

    async def claim_due(self, now: datetime) -> Schedule | None:
        due = [
            s
            for s in self._data.schedules.values()
            if s.enabled and s.next_run_at is not None and s.next_run_at <= now
        ]
        if not due:
            return None
        first = min(due, key=lambda s: s.next_run_at or now)
        return copy.deepcopy(first)

    async def add_run(self, run: ScheduleRun) -> None:
        self._data.runs[run.id] = copy.deepcopy(run)

    async def update_run(self, run: ScheduleRun) -> None:
        if run.id in self._data.runs:
            self._data.runs[run.id] = copy.deepcopy(run)

    async def list_runs(
        self, schedule_id: UUID, account_id: UUID, *, limit: int, before: datetime | None
    ) -> list[ScheduleRun]:
        rows = [
            r
            for r in self._data.runs.values()
            if r.schedule_id == schedule_id
            and r.account_id == account_id
            and (before is None or r.started_at < before)
        ]
        rows.sort(key=lambda r: (-r.started_at.timestamp(), str(r.id)))
        return [copy.deepcopy(r) for r in rows[:limit]]

    async def claim_open_runs(self, limit: int) -> list[ScheduleRun]:
        rows = [
            r for r in self._data.runs.values() if r.outcome == "started" and r.followup is None
        ]
        rows.sort(key=lambda r: r.started_at)
        return [copy.deepcopy(r) for r in rows[:limit]]

    async def put_status(self, status: MowerStatus) -> bool:
        current = self._data.status.get(status.mower_id)
        if current is not None and current.observed_at >= status.observed_at:
            return False
        self._data.status[status.mower_id] = copy.deepcopy(status)
        return True

    async def get_status(self, mower_id: UUID) -> MowerStatus | None:
        return copy.deepcopy(self._data.status.get(mower_id))

    async def put_place(self, place: MowerPlace) -> None:
        self._data.places[place.mower_id] = copy.deepcopy(place)

    async def get_place(self, mower_id: UUID) -> MowerPlace | None:
        return copy.deepcopy(self._data.places.get(mower_id))

    async def put_capabilities(
        self,
        mower_id: UUID,
        account_id: UUID,
        observed_at: datetime,
        capabilities: MowerCapabilities,
    ) -> bool:
        del account_id
        current = self._data.capabilities.get(mower_id)
        if current is not None and current[0] >= observed_at:
            return False
        self._data.capabilities[mower_id] = (observed_at, capabilities)
        return True

    async def get_capabilities(self, mower_id: UUID) -> MowerCapabilities | None:
        found = self._data.capabilities.get(mower_id)
        return found[1] if found else None

    async def get_area_angles(
        self, mower_id: UUID, area_ids: Sequence[str] | None = None
    ) -> list[AreaAngle]:
        wanted = None if area_ids is None else set(area_ids)
        rows = [
            a
            for (mower, area), a in self._data.angles.items()
            if mower == mower_id and (wanted is None or area in wanted)
        ]
        return sorted(rows, key=lambda a: a.area_id)

    async def put_area_angles(self, angles: Sequence[AreaAngle]) -> None:
        for angle in angles:
            self._data.angles[(angle.mower_id, angle.area_id)] = angle

    async def get_rotation(self, mower_id: UUID) -> MowerRotation | None:
        return self._data.rotation.get(mower_id)

    async def put_rotation(self, rotation: MowerRotation) -> None:
        self._data.rotation[rotation.mower_id] = rotation

    async def purge_runs(self, before: datetime) -> int:
        doomed = [
            r.id
            for r in self._data.runs.values()
            if r.started_at < before and (r.outcome != "started" or r.followup is not None)
        ]
        for run_id in doomed:
            del self._data.runs[run_id]
        return len(doomed)

    async def try_lock(self, key: int) -> bool:
        """Transactions run one at a time, so the lock is always free."""
        del key
        return True


class MemoryStore:
    """Implements ``Store`` in memory."""

    def __init__(self) -> None:
        self.data = MemoryData()
        self._lock = asyncio.Lock()

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[MemoryTx]:
        """Serialised transaction with rollback by snapshot."""
        async with self._lock:
            snapshot = copy.deepcopy(self.data)
            try:
                yield MemoryTx(self.data)
            except BaseException:
                self.data.schedules = snapshot.schedules
                self.data.runs = snapshot.runs
                self.data.status = snapshot.status
                self.data.places = snapshot.places
                self.data.capabilities = snapshot.capabilities
                self.data.angles = snapshot.angles
                self.data.rotation = snapshot.rotation
                raise
