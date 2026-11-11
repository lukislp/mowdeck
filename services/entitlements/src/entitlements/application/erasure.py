# SPDX-FileCopyrightText: 2026 Lukas Körber
# SPDX-License-Identifier: AGPL-3.0-only

"""Account erasure (ADR 0082): what Entitlements deletes when an account is deleted for good.

Identity asks on ``account.erase.entitlements`` once the grace period of a deleted account is
over. The use case is the kit's :class:`~mowdeck_service_kit.erasure.AccountErasure` over
Entitlements' own parts:

* the purge is :meth:`EntitlementsService.purge_account`, the one of the demo sandboxes
  (ADR 0076): the account's plan row (it holds the account's name) and its assignment history
  (it holds the actor of each change), in one transaction. No event: the account is gone
  everywhere, nobody needs its plan any more. The plans and the feature catalogue are the
  installation's and stay;
* what is left is counted over every table with an ``account_id`` column
  (:meth:`entitlements.application.ports.Tx.account_rows`). A row that is still there, or a
  purge that raised, is reported as a failure and Identity asks again.

Entitlements keeps nothing outside PostgreSQL and nothing of an account in memory. A later
``entitlements.check`` for the erased account creates no row (an unknown account resolves to
the default plan without one).
"""

from uuid import UUID

from mowdeck_service_kit.erasure import AccountErasure

from entitlements.application.entitlements import EntitlementsService
from entitlements.application.ports import Store


def account_erasure(entitlements: EntitlementsService, store: Store) -> AccountErasure:
    """The erasure of one account in Entitlements."""

    async def leftovers(account_id: UUID) -> dict[str, int]:
        async with store.transaction() as tx:
            return await tx.account_rows(account_id)

    return AccountErasure(entitlements.purge_account, leftovers)
