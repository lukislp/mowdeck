"""Billing: the event of the Billing service (ADR 0031).

Billing handles money, Entitlements decides what an account may do (ADR 0028). Billing keeps the
subscription of an account at a payment provider (Stripe first), sets the account's plan in
Entitlements over ``entitlements.plan.set`` and publishes ``billing.subscription.changed`` on
``event.billing.subscription.changed`` (stream ``EVENTS``) with a :class:`SubscriptionChanged`
payload whenever a provider webhook changed a subscription. Notifications may turn it into a
``billing`` notification later; nothing consumes it yet.

Billing is optional per installation: with ``billing.enabled=false`` (the default) the service is
not started at all and nothing publishes this event.
"""

from enum import StrEnum
from typing import Final
from uuid import UUID

from pydantic import Field

from mowdeck_contracts.v1._base import ContractModel, UtcDateTime
from mowdeck_contracts.v1.entitlements import PlanStatus

BILLING_SUBSCRIPTION_CHANGED: Final = "billing.subscription.changed"
"""Event type: a subscription of an account changed (payload :class:`SubscriptionChanged`)."""


class SubscriptionStatus(StrEnum):
    """Status of a subscription at the payment provider (Stripe's values)."""

    INCOMPLETE = "incomplete"
    """The first payment is pending (expires after 23 hours)."""
    INCOMPLETE_EXPIRED = "incomplete_expired"
    """The first payment never succeeded."""
    TRIALING = "trialing"
    ACTIVE = "active"
    PAST_DUE = "past_due"
    """A renewal payment failed; the provider retries."""
    CANCELED = "canceled"
    UNPAID = "unpaid"
    """The retries failed and the subscription stays open without payment."""
    PAUSED = "paused"
    """A trial ended without a payment method."""


class SubscriptionChanged(ContractModel):
    """Payload of ``billing.subscription.changed``.

    ``status`` is the provider's status, ``plan_status`` what Billing set in Entitlements (None
    when the change did not touch the plan, e.g. ``incomplete`` or ``trial_will_end``).
    ``event_type`` names the provider event that caused it (``customer.subscription.updated``).
    """

    account_id: UUID
    provider: str = Field(min_length=1, max_length=32)
    plan: str | None = Field(default=None, max_length=64)
    status: SubscriptionStatus
    plan_status: PlanStatus | None = None
    current_period_end: UtcDateTime | None = None
    trial_end: UtcDateTime | None = None
    grace_until: UtcDateTime | None = None
    cancel_at_period_end: bool = False
    event_type: str | None = Field(default=None, max_length=100)
