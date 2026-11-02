"""Ports of the virtual zones (ADR 0070)."""

from contextlib import AbstractAsyncContextManager
from datetime import date
from typing import Protocol
from uuid import UUID

from telemetry.domain.zones import Presence, VirtualZone, ZoneDay, ZoneTime


class PresenceTx(Protocol):
    """One evaluation of a mower's presence, holding its row lock."""

    async def presence(self) -> Presence | None:
        """The stored presence (locked until the transaction ends)."""
        ...

    async def put_presence(self, presence: Presence) -> None:
        """Insert or replace the presence."""
        ...

    async def add_time(self, mower_id: UUID, account_id: UUID, time: ZoneTime) -> None:
        """Add ``time`` to the day totals of its zone (a zone deleted meanwhile is skipped)."""
        ...


class ZoneStore(Protocol):
    """Persistence of the zones, the presence and the day totals."""

    async def zones_of(self, account_id: UUID, mower_id: UUID) -> list[VirtualZone]:
        """The zones of a mower of an account, oldest first."""
        ...

    async def zones_for(self, mower_id: UUID) -> list[VirtualZone]:
        """The zones of a mower (ingestion: the state's account was checked before)."""
        ...

    async def add_zone(self, zone: VirtualZone, limit: int) -> bool:
        """Insert a zone; False when the mower has ``limit`` zones already."""
        ...

    async def put_zone(self, zone: VirtualZone) -> bool:
        """Replace a zone of the same account and mower; False when there is none."""
        ...

    async def delete_zone(self, account_id: UUID, mower_id: UUID, zone_id: UUID) -> bool:
        """Delete a zone with its day totals; False when none matched."""
        ...

    def presence_tx(self, mower_id: UUID) -> AbstractAsyncContextManager[PresenceTx]:
        """A transaction that serialises the evaluations of one mower."""
        ...

    async def presence_of(self, account_id: UUID, mower_id: UUID) -> Presence | None:
        """The presence of a mower of an account."""
        ...

    async def days_of(
        self, account_id: UUID, mower_id: UUID, first: date, last: date
    ) -> list[ZoneDay]:
        """Day totals in ``[first, last]``, ordered by zone and day."""
        ...

    async def purge_account(self, account_id: UUID) -> int:
        """Delete every zone, presence and day total of an account (demo sandbox purge, ADR
        0076); returns the number of rows removed."""
        ...
