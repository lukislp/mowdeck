"""Billing models: accounts, customers, subscriptions, invoices, webhook events and the
provider independent view of a provider event (:class:`ProviderEvent`).

No card data ever reaches these models: Checkout and the customer portal run at the provider.
"""

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from uuid import UUID

from mowdeck_contracts.v1 import AccountKind, PlanStatus, SubscriptionStatus


@dataclass(slots=True)
class BillingAccount:
    """An account as Billing knows it (from ``identity.account.created``).

    ``kind`` is None for accounts created before Billing ran: Checkout then offers the business
    fields (tax id, billing address) like for a business.
    """

    account_id: UUID
    name: str | None = None
    kind: AccountKind | None = None
    created_at: datetime | None = None

    @property
    def collects_business_details(self) -> bool:
        """Business (or unknown) accounts give a tax id and a billing address in Checkout."""
        return self.kind is not AccountKind.HOUSEHOLD


@dataclass(slots=True)
class Customer:
    """The provider's customer of an account (one per account and provider)."""

    account_id: UUID
    provider: str
    provider_customer_id: str
    created_at: datetime


@dataclass(slots=True)
class Subscription:
    """A subscription at the provider, as last reported by a webhook."""

    id: UUID
    account_id: UUID
    provider: str
    provider_subscription_id: str
    plan_key: str | None
    status: SubscriptionStatus
    created_at: datetime
    updated_at: datetime
    current_period_end: datetime | None = None
    trial_end: datetime | None = None
    cancel_at_period_end: bool = False
    plan_status: PlanStatus | None = None
    grace_until: datetime | None = None
    last_event_at: datetime | None = None

    @property
    def is_open(self) -> bool:
        """Still billed or recoverable (a new Checkout would create a second subscription)."""
        return self.status not in (
            SubscriptionStatus.CANCELED,
            SubscriptionStatus.INCOMPLETE_EXPIRED,
        )


@dataclass(slots=True)
class Invoice:
    """An invoice reference: amounts and links to the provider's hosted page and PDF."""

    id: UUID
    account_id: UUID
    provider: str
    provider_invoice_id: str
    status: str
    currency: str
    amount_due: int
    amount_paid: int
    issued_at: datetime
    updated_at: datetime
    number: str | None = None
    provider_subscription_id: str | None = None
    hosted_url: str | None = None
    pdf_url: str | None = None


class EventOutcome(StrEnum):
    """What handling a webhook event did."""

    APPLIED = "applied"
    """The subscription or invoice was stored (and the plan set when it changed)."""
    IGNORED = "ignored"
    """An event type Billing does not handle."""
    STALE = "stale"
    """Older than the last event applied to the subscription (delivery order is not guaranteed)."""
    UNKNOWN_ACCOUNT = "unknown_account"
    """No account could be found for the event's customer."""
    REJECTED = "rejected"
    """Entitlements refused the plan (e.g. an unknown plan key); not retried."""
    FAILED = "failed"
    """Handling raised; the provider retries the delivery."""


@dataclass(slots=True)
class WebhookEvent:
    """A received provider event: the exactly-once ledger."""

    provider: str
    event_id: str
    type: str
    received_at: datetime
    attempts: int = 0
    claimed_at: datetime | None = None
    processed_at: datetime | None = None
    outcome: EventOutcome | None = None
    account_id: UUID | None = None


@dataclass(frozen=True, slots=True)
class ProviderSubscription:
    """A subscription as the provider describes it."""

    id: str
    customer_id: str | None
    status: SubscriptionStatus
    price_id: str | None = None
    plan_key: str | None = None
    account_id: UUID | None = None
    current_period_start: datetime | None = None
    current_period_end: datetime | None = None
    ended_at: datetime | None = None
    trial_end: datetime | None = None
    cancel_at_period_end: bool = False


@dataclass(frozen=True, slots=True)
class ProviderInvoice:
    """An invoice as the provider describes it."""

    id: str
    customer_id: str | None
    status: str
    currency: str
    amount_due: int
    amount_paid: int
    created: datetime
    subscription_id: str | None = None
    number: str | None = None
    hosted_url: str | None = None
    pdf_url: str | None = None


@dataclass(frozen=True, slots=True)
class ProviderCheckout:
    """A completed Checkout session."""

    id: str
    customer_id: str | None
    subscription_id: str | None
    account_id: UUID | None
    plan_key: str | None = None


@dataclass(frozen=True, slots=True)
class ProviderEvent:
    """A verified provider webhook event, translated into provider independent parts."""

    id: str
    type: str
    created: datetime
    subscription: ProviderSubscription | None = None
    invoice: ProviderInvoice | None = None
    checkout: ProviderCheckout | None = None
    livemode: bool = False


@dataclass(frozen=True, slots=True)
class PlanPrice:
    """The price of a plan at the provider (None amounts: not known, e.g. not fetched)."""

    plan_key: str
    price_id: str
    currency: str | None = None
    unit_amount: int | None = None
    interval: str | None = None
    interval_count: int | None = None


@dataclass(frozen=True, slots=True)
class OfferedPlan:
    """A plan of Entitlements (name, trial days) as the plan catalogue answers it."""

    key: str
    name: str
    description: str | None = None
    trial_days: int = 0
    features: list[str] = field(default_factory=list)
