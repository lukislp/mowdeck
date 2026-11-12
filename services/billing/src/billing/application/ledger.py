# SPDX-FileCopyrightText: 2026 Lukas Körber
# SPDX-License-Identifier: AGPL-3.0-only

"""Writing the payments ledger (ADR 0083) from provider events.

The webhook use cases call :class:`PaymentLedger` inside the transaction that applies the event,
so a fact is written exactly when the event is applied, and under the same customer lock: an
event of an account that is being erased either writes its fact before the erasure or is
dropped. The facts themselves name no account, so the erasure leaves them alone.

Which events write which fact:

* ``invoice.paid`` of a known customer: a paid invoice (nothing for an invoice of 0).
* ``invoice.payment_failed`` of a known customer: one failed attempt.
* ``customer.subscription.*`` of a known customer: the start once the subscription has started,
  the end once it is canceled. A cancellation of a customer Billing no longer knows (the
  erasure ended the subscription and deleted the customer row before Stripe's event came)
  still writes the end, when the subscription carries MowDeck's metadata.
* ``refund.created``/``updated``/``failed``: the refund in its current status, with the reason
  from its metadata (an account erasure, or anything else). A refund names no customer, and
  most refunds of erasures arrive after the customer is gone, so every refund is recorded.
* ``charge.dispute.created``/``updated``/``closed``: the dispute in its current status.
* Settling a manual refund (the operator paid it back by hand): an erasure refund that
  succeeded.
"""

from datetime import datetime

from mowdeck_service_kit import get_logger

from billing.application.ports import Clock, Store, Tx
from billing.domain.ledger import (
    FactContext,
    FactKind,
    PaymentFact,
    dispute_fact,
    invoice_paid_fact,
    manual_refund_fact,
    payment_failed_fact,
    refs_expire_before,
    refund_fact,
    subscription_facts,
)
from billing.domain.models import (
    ManualRefund,
    ProviderDispute,
    ProviderEvent,
    ProviderInvoice,
    ProviderRefund,
    ProviderSubscription,
)
from billing.domain.prices import PriceTable

_log = get_logger("billing.ledger")


class PaymentLedger:
    """Turns provider events into anonymous facts and writes them."""

    def __init__(
        self, store: Store, provider: str, key: bytes, prices: PriceTable, clock: Clock
    ) -> None:
        self._store = store
        self._provider = provider
        self._key = key
        self._prices = prices
        self._clock = clock

    def _context(self, event_at: datetime | None = None) -> FactContext:
        now = self._clock()
        return FactContext(self._key, self._provider, event_at or now, now)

    async def _write(self, tx: Tx, facts: list[PaymentFact]) -> None:
        for fact in facts:
            if await tx.record_fact(fact):
                _log.info(
                    "payment fact recorded",
                    kind=fact.kind.value,
                    status=fact.status,
                    currency=fact.currency,
                    amount=fact.amount,
                )

    async def _plan_of_invoice(self, tx: Tx, invoice: ProviderInvoice) -> str | None:
        for price in invoice.price_ids:
            plan = self._prices.plan_for(price)
            if plan is not None:
                return plan
        if invoice.subscription_id:
            stored = await tx.get_subscription(self._provider, invoice.subscription_id)
            return stored.plan_key if stored else None
        return None

    async def invoice(self, tx: Tx, event: ProviderEvent, invoice: ProviderInvoice) -> None:
        """A paid invoice or a failed attempt of a known customer's invoice."""
        ctx = self._context(event.created)
        plan = await self._plan_of_invoice(tx, invoice)
        if event.type == "invoice.payment_failed":
            await self._write(tx, [payment_failed_fact(ctx, invoice, plan)])
            return
        paid = invoice_paid_fact(ctx, invoice, plan)
        await self._write(tx, [paid] if paid else [])

    async def subscription(
        self,
        tx: Tx,
        event: ProviderEvent,
        subscription: ProviderSubscription,
        plan_key: str | None,
        *,
        known: bool,
    ) -> None:
        """The start and end of a subscription. Of a customer Billing does not know (``known``
        false) only the end of a subscription that carries MowDeck's metadata."""
        facts = subscription_facts(self._context(event.created), subscription, plan_key)
        if not known:
            if subscription.account_id is None:
                return
            facts = [f for f in facts if f.kind is FactKind.SUBSCRIPTION_ENDED]
        await self._write(tx, facts)

    async def refund(self, event: ProviderEvent, refund: ProviderRefund) -> None:
        """A refund in its current status (its own transaction: it belongs to no customer)."""
        async with self._store.transaction() as tx:
            await self._write(tx, [refund_fact(self._context(event.created), refund)])

    async def dispute(self, event: ProviderEvent, dispute: ProviderDispute) -> None:
        """A dispute in its current status (its own transaction: it names no customer)."""
        async with self._store.transaction() as tx:
            await self._write(tx, [dispute_fact(self._context(event.created), dispute)])

    async def manual_refund(self, tx: Tx, refund: ManualRefund) -> None:
        """A manual refund the operator settled."""
        fact = manual_refund_fact(self._context(refund.settled_at), refund)
        await self._write(tx, [fact] if fact else [])

    async def forget_old_refs(self) -> int:
        """Clear the keyed hashes past :data:`~billing.domain.ledger.REF_HORIZON`; how many."""
        async with self._store.transaction() as tx:
            cleared = await tx.forget_fact_refs(refs_expire_before(self._clock()))
        if cleared:
            _log.info("payment fact references cleared", cleared=cleared)
        return cleared
