"""Demo sandboxes (ADR 0076): forget everything of one expired visitor account.

Identity's cleanup job calls the purge of every service when a sandbox expires. Vision deletes
the account's rows in one transaction and drops what it keeps of the account's mowers in
memory (the bus view and the remembered errors of the automatic analysis).
"""

from collections.abc import Callable, Collection
from uuid import UUID

from vision.application.ports import MowerView, Store


class DemoPurge:
    """Removes one account from the service."""

    def __init__(
        self,
        store: Store,
        view: MowerView | None = None,
        forget_mowers: Callable[[Collection[UUID]], None] | None = None,
    ) -> None:
        self._store = store
        self._view = view
        self._forget_mowers = forget_mowers

    async def purge_account(self, account_id: UUID) -> int:
        """Delete every row of ``account_id``; returns how many (0 on a repeated call). The
        in-memory state does not count: it holds no rows."""
        deleted = await self._store.purge_account(account_id)
        mowers = self._view.forget_account(account_id) if self._view is not None else set()
        if self._forget_mowers is not None:
            self._forget_mowers(mowers)
        return deleted
