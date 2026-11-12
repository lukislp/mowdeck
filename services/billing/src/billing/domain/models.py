# SPDX-FileCopyrightText: 2026 Lukas Körber
# SPDX-License-Identifier: AGPL-3.0-only

"""Billing models: accounts, customers, subscriptions, invoices, webhook events and the
provider independent view of a provider event (:class:`ProviderEvent`).

No card data ever reaches these models: Checkout and the customer portal run at the provider.
"""

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from uuid import UUID

from mowdeck_contracts.v1 import AccountKind, PlanStatus, SubscriptionStatus

ENDED_STATUSES = frozenset({SubscriptionStatus.CANCELED, SubscriptionStatus.INCOMPLETE_EXPIRED})
"""A subscription in one of these is over for good: nothing is billed and it cannot recover."""


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
        return self.status not in ENDED_STATUSES


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
    RECORDED = "recorded"
    """Only an anonymous fact of the payments ledger was written (a dispute): no row of an
    account."""


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
    started_at: datetime | None = None
    """When the subscription began (Stripe's ``start_date``)."""
    canceled_at: datetime | None = None


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
    tax: int | None = None
    """The tax the provider calculated (Stripe Tax), None when it reports none."""
    paid_at: datetime | None = None
    attempt_count: int = 0
    """How many times the provider tried to collect the payment."""
    period_start: datetime | None = None
    """The billing period of the subscription lines (None without such a line)."""
    period_end: datetime | None = None
    price_ids: tuple[str, ...] = ()
    """The prices of the subscription lines, in line order."""


class LineKind(StrEnum):
    """What an invoice line bills (the refund of an erasure counts only ``recurring``)."""

    RECURRING = "recurring"
    """A subscription price for a billing period: paid in advance for a licensed price, in
    arrears (for the period before) for a metered one."""
    PRORATION = "proration"
    """A credit or a charge for a change within a period (an upgrade, a downgrade)."""
    OTHER = "other"
    """A one-off item."""


@dataclass(frozen=True, slots=True)
class InvoiceLine:
    """One line of an invoice: its amount in the currency's smallest unit (before the
    discounts and taxes of the invoice) and the period it bills."""

    amount: int
    kind: LineKind
    period_start: datetime
    period_end: datetime


@dataclass(frozen=True, slots=True)
class PaidInvoice:
    """The latest paid invoice of a subscription with the payment that paid it: what the
    refund of an erased account is computed from (ADR 0082).

    ``amount_paid`` is what the customer actually paid: after discounts, coupons and the credit
    balance, with taxes. ``payment_ref`` is the provider's payment (a Stripe PaymentIntent or
    charge), None when the invoice was paid without one (by the credit balance alone, or
    marked paid outside the provider). ``payment_amount`` is what that payment took."""

    invoice_id: str
    subscription_id: str
    status: str
    currency: str
    amount_paid: int
    lines: tuple[InvoiceLine, ...]
    payment_ref: str | None = None
    payment_amount: int = 0


class RefundStatus(StrEnum):
    """The state of a refund at the provider (Stripe's names)."""

    PENDING = "pending"
    REQUIRES_ACTION = "requires_action"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELED = "canceled"


@dataclass(frozen=True, slots=True)
class ProviderRefund:
    """A refund as the provider describes it."""

    id: str
    payment_ref: str | None
    amount: int
    currency: str
    status: RefundStatus
    metadata: dict[str, str] = field(default_factory=dict)
    failure_reason: str | None = None
    created: datetime | None = None


@dataclass(slots=True)
class ManualRefund:
    """A refund of an account erasure that Billing could not make and the operator pays back by
    hand (ADR 0082, section 5). It names the payment and the invoice at the provider, the amount
    and why, and never the account, a name or an e-mail address: the payment is enough to find
    the payer in the provider's dashboard. ``key`` makes one record per payment and invoice."""

    id: UUID
    key: str
    payment_ref: str
    invoice_ref: str | None
    amount: int
    currency: str
    reason: str
    created_at: datetime
    settled_at: datetime | None = None

    @property
    def is_open(self) -> bool:
        """Not paid back yet."""
        return self.settled_at is None


@dataclass(frozen=True, slots=True)
class ProviderCharge:
    """A charge (one payment) as the provider describes it, e.g. in ``charge.refunded``."""

    id: str
    customer_id: str | None
    currency: str
    amount: int
    amount_refunded: int


@dataclass(frozen=True, slots=True)
class ProviderDispute:
    """A dispute (chargeback) of a payment as the provider describes it: it names the charge,
    never the customer."""

    id: str
    amount: int
    currency: str
    status: str
    """Stripe's names: ``warning_needs_response``, ``needs_response``, ``under_review``,
    ``won``, ``lost``, ``warning_closed``, ``prevented`` and the like."""
    reason: str | None
    """The card network's reason code (``fraudulent``, ``product_not_received``, ...)."""
    created: datetime
    due_by: datetime | None = None
    """The end of the time to submit evidence."""


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
    refund: ProviderRefund | None = None
    charge: ProviderCharge | None = None
    dispute: ProviderDispute | None = None
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
