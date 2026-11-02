"""Demo sandbox purge (ADR 0076): remove everything the Scheduler holds of one demo account.

Identity's cleanup job calls this when a visitor's sandbox expires: schedules and their runs,
and the per-mower facts (activity, map facts, capabilities, mowing angles, rotation step) of
the account's mowers, in one transaction. The Scheduler writes no OpenFGA tuples. Idempotent:
an account without data answers 0.
"""

from uuid import UUID

from scheduler.application.ports import Store


class AccountPurge:
    """Deletes one demo account's rows."""

    def __init__(self, store: Store) -> None:
        self.store = store

    async def purge(self, account_id: UUID) -> int:
        """Remove every row of ``account_id``; returns how many."""
        async with self.store.transaction() as tx:
            return await tx.purge_account(account_id)
