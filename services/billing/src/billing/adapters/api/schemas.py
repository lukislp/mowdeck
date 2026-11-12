# SPDX-FileCopyrightText: 2026 Lukas Körber
# SPDX-License-Identifier: AGPL-3.0-only

"""Request and response bodies of the Billing API."""

from datetime import datetime
from typing import Annotated
from uuid import UUID

from mowdeck_contracts.v1 import PlanStatus, SubscriptionStatus
from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from billing.application.billing import BillingStatus, StatusPlan, SubscriptionSummary
from billing.domain.models import Invoice, ManualRefund, Subscription

PlanKey = Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_-]{0,63}$")]


class PriceOut(BaseModel):
    """A provider price; amounts in the smallest currency unit (cents)."""

    price_id: str
    currency: str | None = Field(description="ISO 4217, lower case; null when not fetched.")
    unit_amount: int | None = Field(description="Amount per interval in cents.")
    interval: str | None = Field(description="day, week, month or year.")
    interval_count: int | None


class StatusPlanOut(BaseModel):
    """A plan on sale."""

    key: str
    name: str | None = Field(description="From Entitlements; null when it is not reachable.")
    description: str | None
    trial_days: int = Field(description="Trial of a first subscription (from Entitlements).")
    price: PriceOut

    @classmethod
    def of(cls, plan: StatusPlan) -> "StatusPlanOut":
        """From the use case result."""
        return cls(
            key=plan.key,
            name=plan.name,
            description=plan.description,
            trial_days=plan.trial_days,
            price=PriceOut(
                price_id=plan.price.price_id,
                currency=plan.price.currency,
                unit_amount=plan.price.unit_amount,
                interval=plan.price.interval,
                interval_count=plan.price.interval_count,
            ),
        )


class BillingStatusOut(BaseModel):
    """``GET /status``: billing runs in this installation and what it sells.

    The service only runs when billing is enabled, so a reachable endpoint always says
    ``enabled: true``; the web app treats a network error or 404 as "billing disabled".
    """

    enabled: bool = True
    provider: str
    currency: str
    automatic_tax: bool = Field(description="Stripe Tax calculates VAT in Checkout.")
    plans: list[StatusPlanOut]

    @classmethod
    def of(cls, status: BillingStatus) -> "BillingStatusOut":
        """From the use case result."""
        return cls(
            provider=status.provider,
            currency=status.currency,
            automatic_tax=status.automatic_tax,
            plans=[StatusPlanOut.of(p) for p in status.plans],
        )


class SubscriptionOut(BaseModel):
    """A subscription as the provider last reported it."""

    plan: str | None
    status: SubscriptionStatus
    plan_status: PlanStatus | None = Field(description="What Billing set in Entitlements.")
    current_period_end: datetime | None
    trial_end: datetime | None
    grace_until: datetime | None
    cancel_at_period_end: bool
    updated_at: datetime

    @classmethod
    def of(cls, s: Subscription) -> "SubscriptionOut":
        """From the domain model."""
        return cls(
            plan=s.plan_key,
            status=s.status,
            plan_status=s.plan_status,
            current_period_end=s.current_period_end,
            trial_end=s.trial_end,
            grace_until=s.grace_until,
            cancel_at_period_end=s.cancel_at_period_end,
            updated_at=s.updated_at,
        )


class SubscriptionSummaryOut(BaseModel):
    """``GET /subscription``: the account's subscription (null before the first checkout)."""

    provider: str
    has_customer: bool = Field(description="The portal can be opened.")
    subscription: SubscriptionOut | None

    @classmethod
    def of(cls, summary: SubscriptionSummary) -> "SubscriptionSummaryOut":
        """From the use case result."""
        return cls(
            provider=summary.provider,
            has_customer=summary.has_customer,
            subscription=SubscriptionOut.of(summary.subscription) if summary.subscription else None,
        )


class InvoiceOut(BaseModel):
    """An invoice reference; the document itself stays at the provider."""

    number: str | None
    status: str
    currency: str
    amount_due: int = Field(description="Cents.")
    amount_paid: int = Field(description="Cents.")
    issued_at: datetime
    hosted_url: str | None
    pdf_url: str | None

    @classmethod
    def of(cls, invoice: Invoice) -> "InvoiceOut":
        """From the domain model."""
        return cls(
            number=invoice.number,
            status=invoice.status,
            currency=invoice.currency,
            amount_due=invoice.amount_due,
            amount_paid=invoice.amount_paid,
            issued_at=invoice.issued_at,
            hosted_url=invoice.hosted_url,
            pdf_url=invoice.pdf_url,
        )


class ManualRefundOut(BaseModel):
    """A refund of an erased account that the operator pays back by hand (ADR 0082). It names
    the payment and the invoice at the provider, never the account."""

    id: UUID
    payment_ref: str = Field(description="The provider's payment (PaymentIntent or charge).")
    invoice_ref: str | None = Field(description="The provider's invoice.")
    amount: int = Field(description="Smallest currency unit (cents; whole yen for JPY).")
    currency: str
    reason: str = Field(description="Why the refund could not be made at the provider.")
    created_at: datetime
    settled_at: datetime | None = Field(description="When the operator marked it paid back.")

    @classmethod
    def of(cls, refund: ManualRefund) -> "ManualRefundOut":
        """From the domain model."""
        return cls(
            id=refund.id,
            payment_ref=refund.payment_ref,
            invoice_ref=refund.invoice_ref,
            amount=refund.amount,
            currency=refund.currency,
            reason=refund.reason,
            created_at=refund.created_at,
            settled_at=refund.settled_at,
        )


class CheckoutIn(BaseModel):
    """Body of ``POST /checkout``."""

    model_config = ConfigDict(extra="forbid")

    plan: PlanKey


class RedirectOut(BaseModel):
    """A provider page to send the browser to."""

    url: str


class WebhookOut(BaseModel):
    """Answer to the provider: received (and what it did, for the logs of the provider)."""

    received: bool = True
    event_id: str
    outcome: str | None
    duplicate: bool
