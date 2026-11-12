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
     (:class:`~billing.application.ports.PaymentProvider`), for every subscription of the
     account that is still open: the unused time paid on each of its paid invoices that bill
     time containing now (the period's invoice, and an upgrade billed on its own) is refunded
     to the original payment method (the rule is :mod:`billing.domain.refunds`), then the
     subscription ends now (not at the period end, no final invoice, no proration). After all
     subscriptions the customer object is deleted. What the provider must keep by law, the
     invoices and the payments with their refunds, stays with the provider.
  2. **Billing's rows,** afterwards and in one transaction: the customer, the subscriptions,
     the invoice references, the ledger rows of the account's provider events and the
     account's name and kind. In the same transaction, a refund that was skipped is recorded
     in ``manual_refunds`` (payment, invoice, amount, reason; no account) for the operator to
     pay back by hand (:mod:`billing.application.manual_refunds`).

  The provider comes first because the rows are what names the customer and the
  subscriptions: a provider call that fails leaves them, the erasure is reported as failed and
  the next attempt knows again what to refund, end and delete. Every provider step is
  idempotent, so the attempt after a half finished one succeeds and never refunds twice (the
  refund is found again by its metadata and is made under an idempotency key). A refund that
  fails stops the erasure before its subscription ends; one that cannot be made for good is
  skipped, recorded and logged as a warning (:func:`billing.domain.refunds.skipped`). **Billing
  never confirms while the customer may still exist at the provider.**
* what is left is counted over every table with an ``account_id`` column
  (:meth:`billing.application.ports.Tx.account_rows`). A row that is still there, or a purge
  that raised, is reported as a failure and Identity asks again.

Billing keeps nothing of an account in memory (the one cache holds the provider's prices), so
there is nothing to forget. Provider events that arrive after the erasure name a customer
Billing no longer knows and are dropped (:mod:`billing.application.billing`).
"""

from datetime import datetime
from uuid import UUID

from mowdeck_service_kit import get_logger
from mowdeck_service_kit.erasure import AccountErasure

from billing.application.ports import Clock, PaymentProvider, Store
from billing.domain.erasure import unreachable_providers
from billing.domain.errors import ProviderError, RefundFailed, RefundImpossible
from billing.domain.models import PaidInvoice, ProviderRefund, RefundStatus
from billing.domain.refunds import (
    RefundAction,
    RefundPlan,
    is_initiated,
    manual_refund,
    plan_refund,
    skipped,
)

_log = get_logger("billing.erasure")


class AccountPurge:
    """Everything Billing holds of one account: at the payment provider, then in its tables."""

    def __init__(self, store: Store, provider: PaymentProvider, clock: Clock) -> None:
        self._store = store
        self._provider = provider
        self._clock = clock

    async def purge(self, account_id: UUID) -> int:
        """Refund the unused part of each paid period and end the open subscriptions, delete the
        customer at the provider, then delete the rows; the number of rows removed (0 when
        Billing never knew the account). Raises ``ProviderError`` when the provider did not
        confirm (``RefundFailed`` for a refund that failed), and deletes no row then."""
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
        now = self._clock()
        refunds: list[RefundPlan] = []
        for subscription in ended:
            subscription_id = subscription.provider_subscription_id
            invoices = await self._provider.paid_invoices_covering(subscription_id, now)
            for invoice in invoices:
                refunds.append(await self._refund(account_id, invoice, now))
            await self._provider.cancel_subscription(subscription_id)
        deleted = [c for c in customers if c.provider == configured]
        for customer in deleted:
            await self._provider.delete_customer(customer.provider_customer_id)
        manual = [manual_refund(p, now) for p in refunds if p.action is RefundAction.SKIP]
        async with self._store.transaction() as tx:
            rows = await tx.purge_account(account_id)
            # In the same transaction: the record of what the operator must pay back exists
            # exactly when the rows that named the payment are gone.
            for record in manual:
                await tx.add_manual_refund(record)
        _log.info(
            "billing data of an account deleted",
            account_id=str(account_id),
            provider=configured,
            subscriptions_ended=len(ended),
            refunds=[f"{p.action.value} {p.amount} {p.currency}".strip() for p in refunds],
            manual_refunds=len(manual),
            customers_deleted=len(deleted),
            rows=rows,
        )
        return rows

    async def _refund(self, account_id: UUID, invoice: PaidInvoice, now: datetime) -> RefundPlan:
        """Refund the unused time paid on one invoice, or find the refund an earlier attempt
        made. Raises ``RefundFailed`` when the provider reports the new refund as failed or
        canceled: the subscription is not ended then, and the next attempt decides again, per
        invoice (:func:`~billing.domain.refunds.plan_refund`)."""
        earlier: list[ProviderRefund] = []
        if invoice.payment_ref is not None:
            earlier = await self._provider.refunds_of(invoice.payment_ref)
        plan = plan_refund(account_id, invoice, earlier, now)
        if plan.action is RefundAction.CREATE and plan.payment_ref is not None:
            try:
                made = await self._provider.refund(
                    plan.payment_ref,
                    plan.amount,
                    plan.currency,
                    plan.idempotency_key,
                    plan.metadata,
                )
            except RefundImpossible as exc:
                plan = skipped(plan, exc.message)
            else:
                if not is_initiated(made):
                    msg = (
                        f"the refund of {plan.amount} {plan.currency} for invoice "
                        f"{invoice.invoice_id} is {made.status.value}"
                    )
                    raise RefundFailed(msg)
                plan = RefundPlan(
                    RefundAction.CREATE,
                    amount=made.amount,
                    currency=made.currency,
                    payment_ref=plan.payment_ref,
                    invoice_id=plan.invoice_id,
                    existing=made,
                )
        _report(account_id, invoice.subscription_id, plan)
        return plan


def _report(account_id: UUID, subscription_id: str, plan: RefundPlan) -> None:
    """Log the refund of one subscription; the operator acts on the two warnings."""
    fields: dict[str, object] = {
        "account_id": str(account_id),
        "subscription_id": subscription_id,
        "amount": plan.amount,
        "currency": plan.currency,
        "payment": plan.payment_ref,
        "invoice": plan.invoice_id,
    }
    if plan.action is RefundAction.SKIP:
        _log.warning(
            "refund of an erased account skipped: recorded to pay it out by hand",
            reason=plan.reason,
            **fields,
        )
    elif plan.existing is not None and plan.existing.status is RefundStatus.REQUIRES_ACTION:
        _log.warning(
            "refund of an erased account waits for the customer: the provider asked them for "
            "the details it needs; if it is canceled, it is recorded to pay it out by hand",
            refund=plan.existing.id,
            **fields,
        )
    elif plan.existing is not None:
        _log.info(
            "refund of an erased account made",
            refund=plan.existing.id,
            status=plan.existing.status.value,
            action=plan.action.value,
            **fields,
        )


def account_erasure(store: Store, provider: PaymentProvider, clock: Clock) -> AccountErasure:
    """The erasure of one account in Billing."""

    async def leftovers(account_id: UUID) -> dict[str, int]:
        async with store.transaction() as tx:
            return await tx.account_rows(account_id)

    return AccountErasure(AccountPurge(store, provider, clock).purge, leftovers)
