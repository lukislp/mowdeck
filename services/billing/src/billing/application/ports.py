"""Ports: what the use cases need from the outside world, as protocols.

:class:`PaymentProvider` is the extension point of ADR 0031: Stripe first (Checkout and the
customer portal), other providers such as Paddle or invoices behind the same interface.
"""

from contextlib import AbstractAsyncContextManager
from datetime import datetime
from typing import Protocol
from uuid import UUID

from mowdeck_contracts.v1 import PlanSetReply, PlanSetRequest, SubscriptionChanged

from billing.domain.models import (
    BillingAccount,
    Customer,
    EventOutcome,
    Invoice,
    OfferedPlan,
    PlanPrice,
    ProviderEvent,
    ProviderSubscription,
    Subscription,
    WebhookEvent,
)


class Clock(Protocol):
    """Returns the current time (timezone-aware)."""

    def __call__(self) -> datetime:
        """The current time."""
        ...


class PaymentProvider(Protocol):
    """A payment provider. No card data passes through MowDeck: payment pages are hosted."""

    @property
    def name(self) -> str:
        """Short name stored with customers and subscriptions (``stripe``, ``fake``)."""
        ...

    async def create_customer(self, account: BillingAccount) -> str:
        """Create the provider's customer of an account (metadata ``account_id``); its id."""
        ...

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
        """Start a hosted checkout for a subscription to ``plan``; the URL to redirect to."""
        ...

    async def create_portal(self, customer_id: str, return_url: str) -> str:
        """Open the hosted customer portal (payment method, invoices, cancel); its URL."""
        ...

    def parse_webhook(self, payload: bytes, signature: str | None) -> ProviderEvent:
        """Verify the signature of a webhook and translate it (``InvalidSignature`` if not)."""
        ...

    async def fetch_subscription(self, subscription_id: str) -> ProviderSubscription:
        """The current state of a subscription at the provider."""
        ...

    async def cancel_subscription(self, subscription_id: str) -> ProviderSubscription:
        """Cancel a subscription now (e.g. when an account is deleted)."""
        ...

    async def fetch_price(self, plan_key: str, price_id: str) -> PlanPrice:
        """Amount, currency and interval of a price."""
        ...


class Tx(Protocol):
    """One database transaction."""

    async def get_account(self, account_id: UUID) -> BillingAccount | None:
        """An account Billing knows."""
        ...

    async def save_account_if_absent(self, account: BillingAccount) -> bool:
        """Insert unless known; True if inserted."""
        ...

    async def get_customer(self, account_id: UUID, provider: str) -> Customer | None:
        """The provider's customer of an account."""
        ...

    async def find_customer(self, provider: str, provider_customer_id: str) -> Customer | None:
        """The customer row with this provider id."""
        ...

    async def save_customer(self, customer: Customer) -> None:
        """Insert the customer unless the account has one for the provider."""
        ...

    async def get_subscription(
        self, provider: str, provider_subscription_id: str
    ) -> Subscription | None:
        """One subscription by its provider id."""
        ...

    async def save_subscription(self, subscription: Subscription) -> None:
        """Insert or update (by provider and provider id)."""
        ...

    async def subscriptions_of(self, account_id: UUID) -> list[Subscription]:
        """Every subscription of an account, newest first."""
        ...

    async def save_invoice(self, invoice: Invoice) -> None:
        """Insert or update (by provider and provider id)."""
        ...

    async def invoices_of(self, account_id: UUID, limit: int) -> list[Invoice]:
        """The newest invoices of an account first."""
        ...

    async def record_event(self, event: WebhookEvent) -> None:
        """Insert the event unless the provider event id is known."""
        ...

    async def claim_event(
        self, provider: str, event_id: str, now: datetime, lease_seconds: float
    ) -> bool:
        """Take the event for processing: not processed and not claimed within the lease.
        Counts the attempt. False when it is processed or claimed by another delivery."""
        ...

    async def finish_event(
        self,
        provider: str,
        event_id: str,
        outcome: EventOutcome,
        processed_at: datetime | None,
        account_id: UUID | None,
    ) -> None:
        """Store the outcome; ``processed_at=None`` releases the claim for a retry."""
        ...

    async def get_event(self, provider: str, event_id: str) -> WebhookEvent | None:
        """One ledger row."""
        ...


class Store(Protocol):
    """Opens transactions."""

    def transaction(self) -> AbstractAsyncContextManager[Tx]:
        """A transaction: committed on normal exit, rolled back on an exception."""
        ...


class EntitlementsGateway(Protocol):
    """Sets plans in Entitlements (``entitlements.plan.set``)."""

    async def set_plan(self, request: PlanSetRequest) -> PlanSetReply:
        """Send the request; raises when Entitlements does not answer (the webhook is retried)."""
        ...


class PlanCatalog(Protocol):
    """The offered plans of Entitlements (names and trial days)."""

    async def offered(self) -> list[OfferedPlan]:
        """The active plans; raises when Entitlements cannot be reached."""
        ...


class EventPublisher(Protocol):
    """Publishes ``billing.subscription.changed``."""

    async def subscription_changed(self, change: SubscriptionChanged) -> None:
        """Publish; failures are logged, never raised (the change is committed)."""
        ...
