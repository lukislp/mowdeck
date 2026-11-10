# SPDX-FileCopyrightText: 2026 Lukas Körber
# SPDX-License-Identifier: AGPL-3.0-only

"""Account erasure (ADR 0082): what the Planner deletes when an account is deleted for good.

Identity asks on ``account.erase.planner`` once the grace period of a deleted account is over,
after Fleet removed the mowers. The use case is the kit's
:class:`~mowdeck_service_kit.erasure.AccountErasure` over the Planner's own parts:

* the purge is :class:`~planner.application.demo_purge.AccountPurge`, the one of the demo
  sandboxes (ADR 0076): the account's templates and the map and capability facts of its mowers,
  in one transaction;
* what is left is counted over every table with an ``account_id`` column
  (:meth:`planner.application.ports.Tx.account_rows`). A row that is still there, or a purge that
  raised, is reported as a failure and Identity asks again.

The Planner writes no OpenFGA tuples, keeps nothing outside PostgreSQL and nothing of an
account in memory.
"""

from uuid import UUID

from mowdeck_service_kit.erasure import AccountErasure

from planner.application.demo_purge import AccountPurge
from planner.application.ports import Store


def account_erasure(purge: AccountPurge, store: Store) -> AccountErasure:
    """The erasure of one account in the Planner."""

    async def leftovers(account_id: UUID) -> dict[str, int]:
        async with store.transaction() as tx:
            return await tx.account_rows(account_id)

    return AccountErasure(purge.purge, leftovers)
