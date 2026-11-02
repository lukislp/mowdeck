"""Demo sandboxes (ADR 0076): forget everything of one expired visitor account.

Identity's cleanup job calls the purge of every service when a sandbox expires. Notifications
deletes the account's rows in one transaction and then drops what the use cases keep per mower
in memory (photo budget, command throttle, Live Activity throttles and area names).
"""

from collections.abc import Callable, Collection, Sequence
from uuid import UUID

from notifications.application.ports import Store


class DemoPurge:
    """Removes one account from the service."""

    def __init__(
        self, store: Store, forget: Sequence[Callable[[Collection[UUID]], None]] = ()
    ) -> None:
        self._store = store
        self._forget = tuple(forget)

    async def purge_account(self, account_id: UUID) -> int:
        """Delete every row of ``account_id``; returns how many (0 on a repeated call)."""
        result = await self._store.purge_account(account_id)
        for forget in self._forget:
            forget(result.mower_ids)
        return result.deleted
