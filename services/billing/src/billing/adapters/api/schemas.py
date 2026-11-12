# SPDX-FileCopyrightText: 2026 Lukas Körber
# SPDX-License-Identifier: AGPL-3.0-only

"""Request and response bodies of the Billing API."""

from datetime import datetime
from typing import Annotated
from uuid import UUID

from mowdeck_contracts.v1 import PlanStatus, SubscriptionStatus
from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from billing.application.billing import BillingStatus, StatusPlan, SubscriptionSummary
from billing.application.payments import PaymentsSummary
from billing.domain.currency import minor_unit_exponent
from billing.domain.ledger import PaymentFact
from billing.domain.models import Invoice, ManualRefund, PlanPrice, Subscription
from billing.domain.statistics import CurrencyRevenue, monthly_amount, round_half_up

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


class CurrencyOut(BaseModel):
    """A currency the dashboard shows amounts in."""

    code: str = Field(description="ISO 4217, lower case.")
    exponent: int = Field(
        description="Decimals of the provider amount: 2 for EUR (990 is 9.90), 0 for JPY, "
        "3 for KWD (Stripe's rules)."
    )


class RevenueMonthOut(BaseModel):
    """One month of one currency, amounts in the smallest unit."""

    month: str = Field(description="YYYY-MM in UTC.")
    gross: int = Field(description="Paid invoices, tax included.")
    tax: int = Field(description="Tax in the paid invoices (0 without Stripe Tax).")
    refunded: int = Field(description="Refunds created this month that take money back.")
    net: int = Field(description="Gross minus refunded.")
    invoices: int
    refunds: int


class CurrencyRevenueOut(BaseModel):
    """The revenue months of one currency (oldest first) and the window's totals."""

    currency: str
    gross: int
    refunded: int
    net: int
    months: list[RevenueMonthOut]

    @classmethod
    def of(cls, row: CurrencyRevenue) -> "CurrencyRevenueOut":
        """From the domain result."""
        return cls(
            currency=row.currency,
            gross=row.gross,
            refunded=row.refunded,
            net=row.net,
            months=[
                RevenueMonthOut(
                    month=m.month,
                    gross=m.gross,
                    tax=m.tax,
                    refunded=m.refunded,
                    net=m.net,
                    invoices=m.invoices,
                    refunds=m.refunds,
                )
                for m in row.months
            ],
        )


class PlanCountOut(BaseModel):
    """Subscriptions of one plan now (null plan: not known)."""

    plan: str | None
    trialing: int
    active: int
    past_due: int


class MrrOut(BaseModel):
    """Monthly recurring revenue of one currency (ADR 0083: list prices of active and past-due
    subscriptions normalised to a month, yearly divided by 12)."""

    currency: str
    amount: int = Field(description="Smallest unit, rounded half up.")
    subscriptions: int


class FlowMonthOut(BaseModel):
    """Subscriptions started and ended in a month."""

    month: str
    started: int
    ended: int


class SubscriptionStatsOut(BaseModel):
    """Subscriptions: live counts per plan, MRR and the monthly flow."""

    plans: list[PlanCountOut]
    mrr: list[MrrOut]
    unpriced: int = Field(description="Billed subscriptions whose plan has no known price.")
    flow: list[FlowMonthOut]


class RefundMonthOut(BaseModel):
    """Refunds that take money back, per month, currency and reason."""

    month: str
    currency: str
    reason: str = Field(description="erasure (an account erasure, ADR 0082) or other.")
    count: int
    amount: int


class FailureMonthOut(BaseModel):
    """Failed payment attempts per month and currency."""

    month: str
    currency: str
    count: int
    amount: int


class FailedPaymentOut(BaseModel):
    """One failed attempt (no invoice, no customer)."""

    at: datetime
    currency: str | None
    amount: int
    plan: str | None
    attempt: int | None

    @classmethod
    def of(cls, fact: PaymentFact) -> "FailedPaymentOut":
        """From a ledger fact."""
        return cls(
            at=fact.occurred_at,
            currency=fact.currency,
            amount=fact.amount,
            plan=fact.plan_key,
            attempt=fact.attempt,
        )


class InvoiceBacklogOut(BaseModel):
    """Open invoices of one currency; overdue ones are open after their due time."""

    currency: str
    open_count: int
    open_amount: int
    overdue_count: int
    overdue_amount: int


class DisputeMonthOut(BaseModel):
    """Disputes opened in a month, per currency, and how many of them were lost."""

    month: str
    currency: str
    opened: int
    amount: int
    lost: int


class DisputeOut(BaseModel):
    """One dispute (no charge, no customer)."""

    opened_at: datetime
    currency: str | None
    amount: int
    status: str | None = Field(description="Stripe's dispute status.")
    reason: str | None = Field(description="The card network's reason code.")
    due_by: datetime | None = Field(description="End of the time to submit evidence.")
    closed_at: datetime | None

    @classmethod
    def of(cls, fact: PaymentFact) -> "DisputeOut":
        """From a ledger fact."""
        return cls(
            opened_at=fact.occurred_at,
            currency=fact.currency,
            amount=fact.amount,
            status=fact.status,
            reason=fact.reason,
            due_by=fact.due_by,
            closed_at=fact.closed_at,
        )


class PaymentProblemsOut(BaseModel):
    """Failed payments, open and overdue invoices, past-due subscriptions and disputes."""

    failures: list[FailureMonthOut]
    recent_failures: list[FailedPaymentOut]
    invoices: list[InvoiceBacklogOut]
    past_due_subscriptions: int
    disputes: list[DisputeMonthOut]
    open_disputes: list[DisputeOut]
    closed_disputes: list[DisputeOut]


class PaymentsSummaryOut(BaseModel):
    """``GET /operator/payments``: the operator's payments dashboard (ADR 0083). Aggregates of
    Billing's own data; no account, customer or provider object id."""

    generated_at: datetime
    time_zone: str = Field(description="The time zone of the month buckets (UTC).")
    months: list[str] = Field(description="The window, oldest first (YYYY-MM).")
    since: datetime | None = Field(
        description="The oldest fact of the ledger: the statistics start with the deploy that "
        "added it; null while there is none."
    )
    currencies: list[CurrencyOut]
    revenue: list[CurrencyRevenueOut]
    subscriptions: SubscriptionStatsOut
    refunds: list[RefundMonthOut]
    problems: PaymentProblemsOut

    @classmethod
    def of(cls, s: PaymentsSummary) -> "PaymentsSummaryOut":
        """From the use case result."""
        return cls(
            generated_at=s.generated_at,
            time_zone=s.time_zone,
            months=s.months,
            since=s.since,
            currencies=[
                CurrencyOut(code=c, exponent=minor_unit_exponent(c)) for c in sorted(s.currencies)
            ],
            revenue=[CurrencyRevenueOut.of(r) for r in s.revenue],
            subscriptions=SubscriptionStatsOut(
                plans=[
                    PlanCountOut(
                        plan=p.plan_key, trialing=p.trialing, active=p.active, past_due=p.past_due
                    )
                    for p in s.plans
                ],
                mrr=[
                    MrrOut(currency=m.currency, amount=m.amount, subscriptions=m.subscriptions)
                    for m in s.mrr.currencies
                ],
                unpriced=s.mrr.unpriced,
                flow=[
                    FlowMonthOut(month=f.month, started=f.started, ended=f.ended) for f in s.flow
                ],
            ),
            refunds=[
                RefundMonthOut(
                    month=r.month,
                    currency=r.currency,
                    reason=r.reason,
                    count=r.count,
                    amount=r.amount,
                )
                for r in s.refunds
            ],
            problems=PaymentProblemsOut(
                failures=[
                    FailureMonthOut(
                        month=f.month, currency=f.currency, count=f.count, amount=f.amount
                    )
                    for f in s.failures
                ],
                recent_failures=[FailedPaymentOut.of(f) for f in s.recent_failures],
                invoices=[
                    InvoiceBacklogOut(
                        currency=b.currency,
                        open_count=b.open_count,
                        open_amount=b.open_amount,
                        overdue_count=b.overdue_count,
                        overdue_amount=b.overdue_amount,
                    )
                    for b in s.backlog
                ],
                past_due_subscriptions=s.past_due,
                disputes=[
                    DisputeMonthOut(
                        month=d.month,
                        currency=d.currency,
                        opened=d.opened,
                        amount=d.amount,
                        lost=d.lost,
                    )
                    for d in s.disputes
                ],
                open_disputes=[DisputeOut.of(f) for f in s.open_disputes],
                closed_disputes=[DisputeOut.of(f) for f in s.closed_disputes],
            ),
        )


class OperatorPriceOut(BaseModel):
    """A configured price as Stripe has it (read only: prices are maintained in Stripe)."""

    plan: str = Field(description="The plan key in the price file.")
    price_id: str
    currency: str | None = Field(description="Null when the price could not be read.")
    unit_amount: int | None
    interval: str | None
    interval_count: int | None
    exponent: int | None = Field(description="Decimals of the amount (see CurrencyOut).")
    monthly_amount: int | None = Field(description="Normalised to a month as in the MRR.")

    @classmethod
    def of(cls, price: PlanPrice) -> "OperatorPriceOut":
        """From a fetched price."""
        monthly = monthly_amount(price)
        return cls(
            plan=price.plan_key,
            price_id=price.price_id,
            currency=price.currency,
            unit_amount=price.unit_amount,
            interval=price.interval,
            interval_count=price.interval_count,
            exponent=minor_unit_exponent(price.currency) if price.currency else None,
            monthly_amount=round_half_up(monthly) if monthly is not None else None,
        )


class OperatorPricesOut(BaseModel):
    """``GET /operator/prices``: every price of the price file, also of plans Entitlements does
    not offer (the plans page warns about those)."""

    currency: str = Field(description="The display currency of the price file.")
    prices: list[OperatorPriceOut]


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
