"""In-memory zone store (tests) with the semantics of the PostgreSQL store."""

import asyncio
from collections import defaultdict
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import date
from uuid import UUID

from telemetry.domain.zones import Presence, VirtualZone, ZoneDay, ZoneTime


class _MemoryPresenceTx:
    def __init__(self, store: "MemoryZoneStore", mower_id: UUID) -> None:
        self._store = store
        self._mower_id = mower_id

    async def presence(self) -> Presence | None:
        return self._store.presences.get(self._mower_id)

    async def put_presence(self, presence: Presence) -> None:
        self._store.presences[presence.mower_id] = presence

    async def add_time(self, mower_id: UUID, account_id: UUID, time: ZoneTime) -> None:
        if time.zone_id not in self._store.zones:
            return
        key = (mower_id, time.zone_id, time.day)
        current = self._store.days.get(key) or ZoneDay(mower_id, account_id, time.zone_id, time.day)
        self._store.days[key] = current.plus(time)


class MemoryZoneStore:
    """The :class:`~telemetry.application.zone_ports.ZoneStore` port in memory."""

    def __init__(self) -> None:
        self.zones: dict[UUID, VirtualZone] = {}
        self.presences: dict[UUID, Presence] = {}
        self.days: dict[tuple[UUID, UUID, date], ZoneDay] = {}
        self._locks: defaultdict[UUID, asyncio.Lock] = defaultdict(asyncio.Lock)

    async def zones_of(self, account_id: UUID, mower_id: UUID) -> list[VirtualZone]:
        return sorted(
            (
                z
                for z in self.zones.values()
                if z.account_id == account_id and z.mower_id == mower_id
            ),
            key=lambda z: (z.created_at, str(z.id)),
        )

    async def zones_for(self, mower_id: UUID) -> list[VirtualZone]:
        return sorted(
            (z for z in self.zones.values() if z.mower_id == mower_id),
            key=lambda z: (z.created_at, str(z.id)),
        )

    async def add_zone(self, zone: VirtualZone, limit: int) -> bool:
        async with self._locks[zone.mower_id]:
            if sum(1 for z in self.zones.values() if z.mower_id == zone.mower_id) >= limit:
                return False
            self.zones[zone.id] = zone
            return True

    async def put_zone(self, zone: VirtualZone) -> bool:
        found = self.zones.get(zone.id)
        if found is None or (found.account_id, found.mower_id) != (zone.account_id, zone.mower_id):
            return False
        self.zones[zone.id] = zone
        return True

    async def delete_zone(self, account_id: UUID, mower_id: UUID, zone_id: UUID) -> bool:
        found = self.zones.get(zone_id)
        if found is None or (found.account_id, found.mower_id) != (account_id, mower_id):
            return False
        del self.zones[zone_id]
        self.days = {k: v for k, v in self.days.items() if k[1] != zone_id}
        return True

    @asynccontextmanager
    async def presence_tx(self, mower_id: UUID) -> AsyncIterator[_MemoryPresenceTx]:
        async with self._locks[mower_id]:
            yield _MemoryPresenceTx(self, mower_id)

    async def presence_of(self, account_id: UUID, mower_id: UUID) -> Presence | None:
        found = self.presences.get(mower_id)
        return found if found is not None and found.account_id == account_id else None

    async def days_of(
        self, account_id: UUID, mower_id: UUID, first: date, last: date
    ) -> list[ZoneDay]:
        return sorted(
            (
                d
                for d in self.days.values()
                if d.account_id == account_id and d.mower_id == mower_id and first <= d.day <= last
            ),
            key=lambda d: (str(d.zone_id), d.day),
        )

    async def purge_account(self, account_id: UUID) -> int:
        before = len(self.zones) + len(self.presences) + len(self.days)
        gone = {k for k, v in self.zones.items() if v.account_id == account_id}
        self.zones = {k: v for k, v in self.zones.items() if k not in gone}
        self.presences = {k: v for k, v in self.presences.items() if v.account_id != account_id}
        self.days = {
            k: v for k, v in self.days.items() if v.account_id != account_id and k[1] not in gone
        }
        return before - len(self.zones) - len(self.presences) - len(self.days)

    def drop_mower(self, mower_id: UUID) -> None:
        """Forget everything of a mower (the SQL store does this in ``drop_mower``)."""
        self.zones = {k: v for k, v in self.zones.items() if v.mower_id != mower_id}
        self.presences.pop(mower_id, None)
        self.days = {k: v for k, v in self.days.items() if k[0] != mower_id}
