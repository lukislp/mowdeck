"""A payment provider without network for tests and development (``MOWDECK_BILLING_PROVIDER=fake``).

It speaks Stripe's webhook format (same signature check and event layout), so the whole webhook
path can be exercised with signed fixtures (``python -m billing.cli send-fixture``) before the
operator has a Stripe account. Checkout and portal URLs point back to the web app.
"""

import secrets
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

from mowdeck_contracts.v1 import SubscriptionStatus

from billing.adapters.providers.stripe import verify_stripe_payload
from billing.adapters.providers.stripe_events import event_from
from billing.domain.errors import NotFoundError
from billing.domain.models import BillingAccount, PlanPrice, ProviderEvent, ProviderSubscription


@dataclass
class FakeProvider:
    """``PaymentProvider`` in memory; records every call for assertions."""

    webhook_secret: str = "whsec_fake"  # noqa: S105 - a development value, not a secret
    amounts: dict[str, int] = field(default_factory=dict)
    currency: str = "eur"
    name: str = "fake"
    customers: dict[str, UUID] = field(default_factory=dict)
    checkouts: list[dict[str, Any]] = field(default_factory=list)
    portals: list[str] = field(default_factory=list)
    subscriptions: dict[str, ProviderSubscription] = field(default_factory=dict)
    cancelled: list[str] = field(default_factory=list)
    fail_prices: bool = False

    async def create_customer(self, account: BillingAccount) -> str:
        customer_id = f"cus_fake_{account.account_id.hex[:12]}"
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

    async def cancel_subscription(self, subscription_id: str) -> ProviderSubscription:
        found = await self.fetch_subscription(subscription_id)
        self.cancelled.append(subscription_id)
        cancelled = ProviderSubscription(
            id=found.id,
            customer_id=found.customer_id,
            status=SubscriptionStatus.CANCELED,
            price_id=found.price_id,
            plan_key=found.plan_key,
            account_id=found.account_id,
            current_period_end=found.current_period_end,
        )
        self.subscriptions[subscription_id] = cancelled
        return cancelled

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
