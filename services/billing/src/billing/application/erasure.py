# SPDX-FileCopyrightText: 2026 Lukas Körber
# SPDX-License-Identifier: AGPL-3.0-only

"""Account erasure (ADR 0082): what Billing deletes when an account is deleted for good.

Identity asks on ``account.erase.billing`` once the grace period of a deleted account is over
(only where the installation runs Billing), and it asks Billing **before** Entitlements: while
Billing can still set a plan, Entitlements keeps the account's plan row. The use case is the
kit's :class:`~mowdeck_service_kit.erasure.AccountErasure` over Billing's own parts:

* the purge is :class:`AccountPurge`. Billing has no purge of the demo sandboxes to reuse (the
  demo does not run it), so this is the one place that deletes an account's billing data:

  1. **At the payment provider,** through the port
     (:class:`~billing.application.ports.PaymentProvider`): every subscription of the account
     that is still open ends now (not at the period end, no final invoice, no proration, no
     refund), then the customer object is deleted. What the provider must keep by law, the
     invoices, stays with the provider.
  2. **Billing's rows,** afterwards and in one transaction: the customer, the subscriptions,
     the invoice references, the ledger rows of the account's provider events and the
     account's name and kind.

  The provider comes first because the rows are what names the customer and the
  subscriptions: a provider call that fails leaves them, the erasure is reported as failed and
  the next attempt knows again what to end and to delete. Both provider calls are idempotent,
  so the attempt after a half finished one succeeds. **Billing never confirms while the
  customer may still exist at the provider.**
* what is left is counted over every table with an ``account_id`` column
  (:meth:`billing.application.ports.Tx.account_rows`). A row that is still there, or a purge
  that raised, is reported as a failure and Identity asks again.

Billing keeps nothing of an account in memory (the one cache holds the provider's prices), so
there is nothing to forget. Provider events that arrive after the erasure name a customer
Billing no longer knows and are dropped (:mod:`billing.application.billing`).
"""

from uuid import UUID

from mowdeck_service_kit import get_logger
from mowdeck_service_kit.erasure import AccountErasure

from billing.application.ports import PaymentProvider, Store
from billing.domain.erasure import unreachable_providers
from billing.domain.errors import ProviderError

_log = get_logger("billing.erasure")


class AccountPurge:
    """Everything Billing holds of one account: at the payment provider, then in its tables."""

    def __init__(self, store: Store, provider: PaymentProvider) -> None:
        self._store = store
        self._provider = provider

    async def purge(self, account_id: UUID) -> int:
        """End the open subscriptions and delete the customer at the provider, then delete the
        rows; the number of rows removed (0 when Billing never knew the account). Raises
        ``ProviderError`` when the provider did not confirm, and deletes no row then."""
        async with self._store.transaction() as tx:
            customers = await tx.customers_of(account_id)
            open_subscriptions = [s for s in await tx.subscriptions_of(account_id) if s.is_open]
        configured = self._provider.name
        elsewhere = unreachable_providers(
            [c.provider for c in customers] + [s.provider for s in open_subscriptions], configured
        )
        if elsewhere:
            msg = f"the account has a customer at {', '.join(elsewhere)}, not at {configured}"
            raise ProviderError(msg)
        ended = [s for s in open_subscriptions if s.provider == configured]
        for subscription in ended:
            await self._provider.cancel_subscription(subscription.provider_subscription_id)
        deleted = [c for c in customers if c.provider == configured]
        for customer in deleted:
            await self._provider.delete_customer(customer.provider_customer_id)
        async with self._store.transaction() as tx:
            rows = await tx.purge_account(account_id)
        _log.info(
            "billing data of an account deleted",
            account_id=str(account_id),
            provider=configured,
            subscriptions_ended=len(ended),
            customers_deleted=len(deleted),
            rows=rows,
        )
        return rows


def account_erasure(store: Store, provider: PaymentProvider) -> AccountErasure:
    """The erasure of one account in Billing."""

    async def leftovers(account_id: UUID) -> dict[str, int]:
        async with store.transaction() as tx:
            return await tx.account_rows(account_id)

    return AccountErasure(AccountPurge(store, provider).purge, leftovers)
