# SPDX-FileCopyrightText: 2026 Lukas Körber
# SPDX-License-Identifier: AGPL-3.0-only

"""Account erasure (ADR 0082): what the Scheduler deletes when an account is deleted for good.

Identity asks on ``account.erase.scheduler`` once the grace period of a deleted account is
over, after Fleet removed the mowers. The use case is the kit's
:class:`~mowdeck_service_kit.erasure.AccountErasure` over the Scheduler's own parts:

* the purge is :class:`~scheduler.application.demo_purge.AccountPurge`, the one of the demo
  sandboxes (ADR 0076): schedules and their runs and the per-mower facts (activity with the last
  position, map facts, capabilities, mowing angles, rotation step), in one transaction;
* in memory the ingest forgets the statuses it last wrote for the account's mowers (they hold a
  position; the demo purge leaves them to age out);
* what is left is counted over every table with an ``account_id`` column
  (:meth:`scheduler.application.ports.Tx.account_rows`). A row that is still there, or a purge
  that raised, is reported as a failure and Identity asks again.

The Scheduler writes no OpenFGA tuples and keeps nothing outside PostgreSQL.
"""

from uuid import UUID

from mowdeck_service_kit.erasure import AccountErasure

from scheduler.application.demo_purge import AccountPurge
from scheduler.application.ingest import Ingest
from scheduler.application.ports import Store


def account_erasure(purge: AccountPurge, store: Store, ingest: Ingest) -> AccountErasure:
    """The erasure of one account in the Scheduler."""

    async def leftovers(account_id: UUID) -> dict[str, int]:
        async with store.transaction() as tx:
            return await tx.account_rows(account_id)

    return AccountErasure(purge.purge, leftovers, forget=[ingest.forget_account])
