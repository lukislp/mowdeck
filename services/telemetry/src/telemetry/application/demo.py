"""Demo sandboxes (ADR 0076): forget everything of one expired visitor account.

Identity's cleanup job calls the purge of every service when a sandbox expires. Telemetry then
deletes the account's rows in all three stores (telemetry, virtual zones, theft tracking) and
drops what the use cases keep per account in memory (the aerial tile rate limit). The other
in-process caches are keyed by zone, session or rendered content, hold no account and are
unreachable once the rows are gone; they age out on their own.
"""

from collections.abc import Callable, Sequence
from uuid import UUID

from telemetry.application.ports import Store
from telemetry.application.tracking_ports import TrackingStore
from telemetry.application.zone_ports import ZoneStore


class DemoPurge:
    """Removes one account from every store of the service."""

    def __init__(
        self,
        store: Store,
        zones: ZoneStore | None = None,
        tracking: TrackingStore | None = None,
        forget: Sequence[Callable[[UUID], None]] = (),
    ) -> None:
        self._store = store
        self._zones = zones
        self._tracking = tracking
        self._forget = tuple(forget)

    async def purge_account(self, account_id: UUID) -> int:
        """Delete every row of ``account_id``; returns how many (0 on a repeated call)."""
        deleted = 0
        if self._tracking is not None:
            deleted += await self._tracking.purge_account(account_id)
        if self._zones is not None:
            deleted += await self._zones.purge_account(account_id)
        deleted += await self._store.purge_account(account_id)
        for forget in self._forget:
            forget(account_id)
        return deleted
