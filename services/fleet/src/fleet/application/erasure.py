# SPDX-FileCopyrightText: 2026 Lukas Körber
# SPDX-License-Identifier: AGPL-3.0-only

"""Account erasure (ADR 0082): what Fleet deletes when an account is deleted for good.

Identity asks on ``account.erase.fleet`` once the grace period of a deleted account is over.
Fleet is asked first: removing the mowers publishes ``mower.removed`` for each, so the
Device-Gateway closes their connections and the other services see no further states.

The use case is the kit's :class:`~mowdeck_service_kit.erasure.AccountErasure` over Fleet's own
parts:

* the purge is :class:`~fleet.application.demo_purge.AccountPurge`, the one of the demo sandboxes
  (ADR 0076): mowers with their groups, links, area names and tuples, groups and sites (the
  default site too) with their tuples, base stations, linked vendor accounts with their encrypted
  credentials, command records, drive sessions and the account's audit rows, and in memory the
  cached still images and the login throttle's entries;
* what is left is counted over every table with an ``account_id`` column
  (:meth:`fleet.application.ports.Tx.account_rows`). A row that is still there, or a purge that
  raised, is reported as a failure; Identity asks again and the purge continues where it stopped.
"""

from uuid import UUID

from mowdeck_service_kit.erasure import AccountErasure

from fleet.application.demo_purge import AccountPurge
from fleet.application.ports import Store


def account_erasure(purge: AccountPurge, store: Store) -> AccountErasure:
    """The erasure of one account in Fleet."""

    async def leftovers(account_id: UUID) -> dict[str, int]:
        async with store.transaction() as tx:
            return await tx.account_rows(account_id)

    return AccountErasure(purge.purge, leftovers)
