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
     subscriptions, per customer: a **credit balance** the customer holds is paid back as
     refunds over its most recent payments, newest first, the balance is zeroed with a balance
     transaction (the rule is :mod:`billing.domain.credit`), and then the customer object is
     deleted. What the provider must keep by law, the invoices and the payments with their
     refunds, stays with the provider.
  2. **Billing's rows,** afterwards and in one transaction: the customer, the subscriptions,
     the invoice references, the ledger rows of the account's provider events and the
     account's name and kind. In the same transaction, a refund that was skipped, and the part
     of a credit balance no payment could take, is recorded in ``manual_refunds`` (payment,
     invoice, amount, reason; no account) for the operator to pay back by hand
     (:mod:`billing.application.manual_refunds`).

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

from collections.abc import Callable
from datetime import datetime
from uuid import UUID

from mowdeck_service_kit import get_logger
from mowdeck_service_kit.erasure import AccountErasure

from billing.application.ports import Clock, PaymentProvider, Store
from billing.domain.credit import (
    credit_owed,
    credit_records,
    credit_skipped,
    earliest_payment,
    plan_credit_refunds,
)
from billing.domain.erasure import unreachable_providers
from billing.domain.errors import ProviderError, RefundFailed, RefundImpossible
from billing.domain.models import (
    ManualRefund,
    PaidInvoice,
    ProviderRefund,
    RecentPayment,
    RefundStatus,
)
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
        """Refund the unused part of each paid period and end the open subscriptions, pay back
        the credit balance, zero it and delete the customer at the provider, then delete the
        rows; the number of rows removed (0 when Billing never knew the account). Raises
        ``ProviderError`` when the provider did not confirm (``RefundFailed`` for a refund that
        failed), and deletes no row then."""
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
        manual = [manual_refund(p, now) for p in refunds if p.action is RefundAction.SKIP]
        for customer in deleted:
            customer_id = customer.provider_customer_id
            # After every subscription ended (which adds no credit: no proration), before the
            # customer and its balance are gone.
            manual.extend(await self._pay_back_credit(account_id, customer_id, now))
            await self._provider.delete_customer(customer_id)
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
        plan = await self._carry_out(plan, f"invoice {invoice.invoice_id}", skipped)
        _report(account_id, plan, subscription_id=invoice.subscription_id)
        return plan

    async def _pay_back_credit(
        self, account_id: UUID, customer_id: str, now: datetime
    ) -> list[ManualRefund]:
        """Pay back the customer's credit balance against its recent payments, then zero the
        balance; the records of what the operator pays out by hand (written with the rows).
        Raises like :meth:`_refund`; the balance stays then, and the next attempt plans again
        (:func:`~billing.domain.credit.plan_credit_refunds`), finding the refunds it made."""
        balance = await self._provider.customer_credit_balance(customer_id)
        payments: list[tuple[RecentPayment, list[ProviderRefund]]] = []
        if credit_owed(balance) > 0:
            for payment in await self._provider.recent_payments(customer_id, earliest_payment(now)):
                payments.append((payment, await self._provider.refunds_of(payment.payment_ref)))
        plan = plan_credit_refunds(account_id, customer_id, balance, payments)
        if plan.credit <= 0:
            return []
        done: list[RefundPlan] = []
        for refund in plan.refunds:
            refund = await self._carry_out(refund, "the credit balance", credit_skipped)
            _report(account_id, refund, customer_id=customer_id)
            done.append(refund)
        if plan.clear > 0:
            await self._provider.clear_credit_balance(
                customer_id, plan.clear, plan.currency, plan.clear_key, plan.clear_metadata
            )
        records = credit_records(plan, done, now)
        _log.info(
            "credit balance of an erased account paid back",
            account_id=str(account_id),
            customer_id=customer_id,
            credit=plan.credit,
            currency=plan.currency,
            refunded=sum(p.amount for p in done if p.action is not RefundAction.SKIP),
            cleared=plan.clear,
        )
        if plan.leftover:
            _log.warning(
                "credit balance of an erased account left over: recorded to pay it out by hand",
                account_id=str(account_id),
                customer_id=customer_id,
                amount=plan.leftover,
                currency=plan.currency,
                reason=plan.leftover_reason,
            )
        return records

    async def _carry_out(
        self, plan: RefundPlan, what: str, skip: Callable[[RefundPlan, str], RefundPlan]
    ) -> RefundPlan:
        """Make the refund of a ``CREATE`` plan; other plans come back as they are. A permanent
        refusal (``RefundImpossible``) is a skip (``skip`` words its reason); a refund the
        provider answers as failed or canceled raises ``RefundFailed``."""
        if plan.action is not RefundAction.CREATE or plan.payment_ref is None:
            return plan
        try:
            made = await self._provider.refund(
                plan.payment_ref,
                plan.amount,
                plan.currency,
                plan.idempotency_key,
                plan.metadata,
            )
        except RefundImpossible as exc:
            return skip(plan, exc.message)
        if not is_initiated(made):
            msg = f"the refund of {plan.amount} {plan.currency} for {what} is {made.status.value}"
            raise RefundFailed(msg)
        return RefundPlan(
            RefundAction.CREATE,
            amount=made.amount,
            currency=made.currency,
            payment_ref=plan.payment_ref,
            invoice_id=plan.invoice_id,
            existing=made,
        )


def _report(account_id: UUID, plan: RefundPlan, **context: str) -> None:
    """Log one refund (of a subscription's invoice, or of the credit balance of a customer:
    ``context``); the operator acts on the two warnings."""
    fields: dict[str, object] = {
        "account_id": str(account_id),
        **context,
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
