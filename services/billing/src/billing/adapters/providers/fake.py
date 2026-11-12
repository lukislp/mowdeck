# SPDX-FileCopyrightText: 2026 Lukas Körber
# SPDX-License-Identifier: AGPL-3.0-only

"""A payment provider without network for tests and development (``MOWDECK_BILLING_PROVIDER=fake``).

It speaks Stripe's webhook format (same signature check and event layout), so the whole webhook
path can be exercised with signed fixtures (``python -m billing.cli send-fixture``) before the
operator has a Stripe account. Checkout and portal URLs point back to the web app.
"""

import secrets
from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Any
from uuid import UUID

from mowdeck_contracts.v1 import SubscriptionStatus

from billing.adapters.providers.stripe import verify_stripe_payload
from billing.adapters.providers.stripe_events import event_from
from billing.domain.credit import is_clearing_metadata
from billing.domain.errors import NotFoundError, ProviderError
from billing.domain.models import (
    ENDED_STATUSES,
    BillingAccount,
    CreditBalance,
    PaidInvoice,
    PlanPrice,
    ProviderEvent,
    ProviderRefund,
    ProviderSubscription,
    RecentPayment,
    RefundStatus,
)
from billing.domain.refunds import covers, refunded_already


def fake_customer_id(account_id: UUID) -> str:
    """The customer id the fake provider gives an account (stable, so a fixture can name it)."""
    return f"cus_fake_{account_id.hex[:12]}"


@dataclass
class FakeProvider:
    """``PaymentProvider`` in memory; records every call for assertions.

    ``failures`` makes a call fail like the real provider would: the exception stored under a
    method name is raised by that method until the entry is removed. ``calls`` lists every call
    of the erasure methods that change something (``refund``, ``cancel_subscription``,
    ``clear_credit_balance``, ``delete_customer``), failed ones included.

    Refunds: ``paid_invoices`` holds the paid invoices per subscription id (newest first),
    ``refunds`` the refunds per payment. A new refund gets ``refund_status``; an idempotency key
    that was used before answers the refund made with it, like Stripe."""

    webhook_secret: str = "whsec_fake"  # noqa: S105 - a development value, not a secret
    amounts: dict[str, int] = field(default_factory=dict)
    currency: str = "eur"
    name: str = "fake"
    customers: dict[str, UUID] = field(default_factory=dict)
    checkouts: list[dict[str, Any]] = field(default_factory=list)
    portals: list[str] = field(default_factory=list)
    subscriptions: dict[str, ProviderSubscription] = field(default_factory=dict)
    cancelled: list[str] = field(default_factory=list)
    deleted: list[str] = field(default_factory=list)
    calls: list[tuple[str, str]] = field(default_factory=list)
    failures: dict[str, Exception] = field(default_factory=dict)
    fail_prices: bool = False
    paid_invoices: dict[str, list[PaidInvoice]] = field(default_factory=dict)
    refunds: dict[str, list[ProviderRefund]] = field(default_factory=dict)
    refund_status: RefundStatus = RefundStatus.SUCCEEDED
    idempotency_keys: dict[str, ProviderRefund] = field(default_factory=dict)
    balances: dict[str, CreditBalance] = field(default_factory=dict)
    payments: dict[str, list[RecentPayment]] = field(default_factory=dict)
    balance_keys: dict[str, int] = field(default_factory=dict)
    """Credit balances and payments per customer id (a customer without an entry has a zero
    balance), and the idempotency keys of the balance transactions with the amount taken."""

    async def create_customer(self, account: BillingAccount) -> str:
        customer_id = fake_customer_id(account.account_id)
        self.customers[customer_id] = account.account_id
        return customer_id

    async def create_checkout(
        self,
        account: BillingAccount,
        customer_id: str,
        plan: str,
        price_id: str,
        success_url: str,
        cancel_url: str,
        trial_days: int = 0,
    ) -> str:
        session = f"cs_fake_{secrets.token_hex(8)}"
        self.checkouts.append(
            {
                "session": session,
                "account_id": account.account_id,
                "customer": customer_id,
                "plan": plan,
                "price": price_id,
                "success_url": success_url,
                "cancel_url": cancel_url,
                "trial_days": trial_days,
                "business": account.collects_business_details,
            }
        )
        return f"{success_url.split('?')[0]}?billing=fake-checkout&session={session}"

    async def create_portal(self, customer_id: str, return_url: str) -> str:
        self.portals.append(customer_id)
        return f"{return_url.split('#')[0]}?billing=fake-portal&customer={customer_id}"

    def parse_webhook(self, payload: bytes, signature: str | None) -> ProviderEvent:
        return event_from(verify_stripe_payload(payload, signature, self.webhook_secret))

    async def fetch_subscription(self, subscription_id: str) -> ProviderSubscription:
        found = self.subscriptions.get(subscription_id)
        if found is None:
            raise NotFoundError(f"unknown subscription {subscription_id}")
        return found

    def _attempt(self, method: str, object_id: str) -> None:
        self.calls.append((method, object_id))
        failure = self.failures.get(method)
        if failure is not None:
            raise failure

    def _end(self, found: ProviderSubscription) -> None:
        self.cancelled.append(found.id)
        self.subscriptions[found.id] = ProviderSubscription(
            id=found.id,
            customer_id=found.customer_id,
            status=SubscriptionStatus.CANCELED,
            price_id=found.price_id,
            plan_key=found.plan_key,
            account_id=found.account_id,
            current_period_end=found.current_period_end,
        )

    async def paid_invoices_covering(
        self, subscription_id: str, now: datetime
    ) -> list[PaidInvoice]:
        failure = self.failures.get("paid_invoices_covering")
        if failure is not None:
            raise failure
        return [i for i in self.paid_invoices.get(subscription_id, []) if covers(i, now)]

    async def refunds_of(self, payment_ref: str) -> list[ProviderRefund]:
        failure = self.failures.get("refunds_of")
        if failure is not None:
            raise failure
        return list(self.refunds.get(payment_ref, []))

    async def refund(
        self,
        payment_ref: str,
        amount: int,
        currency: str,
        idempotency_key: str,
        metadata: dict[str, str],
    ) -> ProviderRefund:
        self._attempt("refund", payment_ref)
        if idempotency_key in self.idempotency_keys:
            return self.idempotency_keys[idempotency_key]
        left = self._payment_amount(payment_ref) - refunded_already(
            self.refunds.get(payment_ref, [])
        )
        if amount <= 0 or amount > left:
            msg = f"refund of {amount} exceeds the {left} left of {payment_ref}"
            raise ProviderError(msg)
        made = ProviderRefund(
            id=f"re_fake_{secrets.token_hex(6)}",
            payment_ref=payment_ref,
            amount=amount,
            currency=currency,
            status=self.refund_status,
            metadata=dict(metadata),
        )
        self.refunds.setdefault(payment_ref, []).append(made)
        self.idempotency_keys[idempotency_key] = made
        return made

    def _payment_amount(self, payment_ref: str) -> int:
        """What a payment took: from a paid invoice, else from a customer's payments."""
        for invoices in self.paid_invoices.values():
            for invoice in invoices:
                if invoice.payment_ref == payment_ref:
                    return invoice.payment_amount
        for payments in self.payments.values():
            for payment in payments:
                if payment.payment_ref == payment_ref:
                    return payment.amount
        return 0

    async def customer_credit_balance(self, customer_id: str) -> CreditBalance | None:
        failure = self.failures.get("customer_credit_balance")
        if failure is not None:
            raise failure
        if customer_id in self.deleted:
            return None
        return self.balances.get(customer_id, CreditBalance(0, self.currency))

    async def recent_payments(self, customer_id: str, since: datetime) -> list[RecentPayment]:
        failure = self.failures.get("recent_payments")
        if failure is not None:
            raise failure
        found = [p for p in self.payments.get(customer_id, []) if p.created >= since]
        return [
            replace(p, refunded=refunded_already(self.refunds.get(p.payment_ref, [])))
            for p in sorted(found, key=lambda p: p.created, reverse=True)
        ]

    async def clear_credit_balance(
        self,
        customer_id: str,
        amount: int,
        currency: str,
        idempotency_key: str,
        metadata: dict[str, str],
    ) -> None:
        self._attempt("clear_credit_balance", customer_id)
        if idempotency_key in self.balance_keys or customer_id in self.deleted:
            return
        if amount <= 0:
            raise ProviderError(f"a balance transaction of {amount} {currency}")
        found = self.balances.get(customer_id, CreditBalance(0, currency))
        cleared = amount if is_clearing_metadata(metadata) else 0
        self.balances[customer_id] = CreditBalance(
            found.amount - amount, found.currency, found.cleared + cleared
        )
        self.balance_keys[idempotency_key] = amount

    async def cancel_subscription(self, subscription_id: str) -> None:
        self._attempt("cancel_subscription", subscription_id)
        found = self.subscriptions.get(subscription_id)
        if found is not None and found.status not in ENDED_STATUSES:
            self._end(found)

    async def delete_customer(self, customer_id: str) -> None:
        self._attempt("delete_customer", customer_id)
        if self.customers.pop(customer_id, None) is None:
            return
        self.deleted.append(customer_id)
        # Like Stripe: deleting a customer ends the subscriptions it still has.
        for found in list(self.subscriptions.values()):
            if found.customer_id == customer_id and found.status not in ENDED_STATUSES:
                self._end(found)

    async def fetch_price(self, plan_key: str, price_id: str) -> PlanPrice:
        if self.fail_prices:
            raise RuntimeError("prices unavailable")
        return PlanPrice(
            plan_key=plan_key,
            price_id=price_id,
            currency=self.currency,
            unit_amount=self.amounts.get(plan_key, 500),
            interval="month",
            interval_count=1,
        )
