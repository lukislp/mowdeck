# SPDX-FileCopyrightText: 2026 Lukas Körber
# SPDX-License-Identifier: AGPL-3.0-only

"""The payments ledger (ADR 0083): anonymous facts about money, written from provider webhooks,
for the statistics of the operator dashboard.

A fact says *what* happened (an invoice was paid, a payment failed, a refund or a dispute was
opened or changed, a subscription started or ended), *how much* in which currency, for which
plan and *when*; never *whose*. It holds no account id, no customer id, no name and no e-mail
address. The provider's object id is not stored either: :func:`ledger_ref` turns it into a
keyed hash (HMAC-SHA256 with the installation's ledger key), which finds the fact again when
the provider repeats an event or reports a later status of the same refund or dispute, and
which nobody can turn back into the object id without the key.

:data:`REF_HORIZON` after a fact last changed even that hash is cleared
(:func:`refs_expire_before`): the fact then is a plain statistic that nothing can link to a
payment any more. Status updates come long before that (a refund within days, a dispute within
about three months).

Facts are never deleted. A refund's or a dispute's status, amount and closing time follow the
provider's newest event (:func:`apply_update`); an older event that arrives late changes
nothing, and the same event twice changes nothing either.

The functions here are pure: the caller passes the key, the provider's name and ``now``.
"""

import hashlib
import hmac
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from enum import StrEnum
from uuid import UUID, uuid4

from mowdeck_contracts.v1 import SubscriptionStatus

from billing.domain.models import (
    ManualRefund,
    ProviderDispute,
    ProviderInvoice,
    ProviderRefund,
    ProviderSubscription,
)
from billing.domain.refunds import INITIATED, is_erasure_metadata

REF_HORIZON = timedelta(days=180)
"""How long the keyed hash of a fact stays after the fact last changed. A refund settles within
days and a dispute closes within about three months; the provider retries a webhook for three
days. After that nothing will update the fact, so the last link to a payment goes."""


class FactKind(StrEnum):
    """What a fact records."""

    INVOICE_PAID = "invoice_paid"
    """An invoice was paid: ``amount`` is what was paid, ``tax`` the tax in it."""
    PAYMENT_FAILED = "payment_failed"
    """One failed attempt to collect an invoice (``attempt`` counts them per invoice)."""
    REFUND = "refund"
    """A refund at the provider, or one the operator paid back by hand."""
    DISPUTE = "dispute"
    """A dispute (chargeback) of a payment."""
    SUBSCRIPTION_STARTED = "subscription_started"
    SUBSCRIPTION_ENDED = "subscription_ended"


class RefundReason(StrEnum):
    """Why money went back."""

    ERASURE = "erasure"
    """The unused part of a paid period of an erased account (ADR 0082), at the provider or
    paid back by hand."""
    OTHER = "other"
    """Any other refund, e.g. one the operator made in the provider's dashboard."""


EFFECTIVE_REFUND_STATUSES = frozenset(status.value for status in INITIATED)
"""A refund in one of these takes money back (or is on its way to): the statuses of
:data:`~billing.domain.refunds.INITIATED`. A failed or canceled one does not count."""

CLOSED_DISPUTE_STATUSES = frozenset({"won", "lost", "warning_closed", "prevented"})
"""A dispute in one of these is decided."""

STARTED_STATUSES = frozenset(
    {
        SubscriptionStatus.TRIALING,
        SubscriptionStatus.ACTIVE,
        SubscriptionStatus.PAST_DUE,
        SubscriptionStatus.UNPAID,
        SubscriptionStatus.PAUSED,
    }
)
"""A subscription in one of these has started (an incomplete one has not: its first payment
never went through)."""


@dataclass(slots=True)
class PaymentFact:
    """One anonymous fact of the ledger. ``ref`` is the keyed hash of the provider's object
    (None once :data:`REF_HORIZON` passed); ``event_at`` is the time of the provider event that
    last changed the fact."""

    id: UUID
    kind: FactKind
    ref: str | None
    provider: str
    occurred_at: datetime
    event_at: datetime
    recorded_at: datetime
    updated_at: datetime
    currency: str | None = None
    amount: int = 0
    tax: int | None = None
    plan_key: str | None = None
    status: str | None = None
    reason: str | None = None
    period_start: datetime | None = None
    period_end: datetime | None = None
    attempt: int | None = None
    closed_at: datetime | None = None
    due_by: datetime | None = None


UPDATABLE = ("amount", "status", "reason", "closed_at", "due_by", "event_at", "updated_at")
"""The fields a later event of the same object may change; the others are written once."""


def ledger_ref(key: bytes, provider: str, kind: FactKind, object_id: str) -> str:
    """The keyed hash that finds the fact of a provider object again (hex HMAC-SHA256 of
    ``provider:kind:object_id``)."""
    message = f"{provider}:{kind.value}:{object_id}".encode()
    return hmac.new(key, message, hashlib.sha256).hexdigest()


def apply_update(stored: PaymentFact, incoming: PaymentFact) -> PaymentFact | None:
    """The stored fact after a later event of the same object, or None when nothing changes:
    an event older than the one that last changed the fact is stale, and one that brings the
    same values changes nothing."""
    if incoming.event_at < stored.event_at:
        return None
    if all(
        getattr(stored, name) == getattr(incoming, name)
        for name in UPDATABLE
        if name not in ("event_at", "updated_at")
    ):
        return None
    return replace(stored, **{name: getattr(incoming, name) for name in UPDATABLE})


def refs_expire_before(now: datetime) -> datetime:
    """Facts unchanged since before this lose their keyed hash."""
    return now - REF_HORIZON


@dataclass(frozen=True, slots=True)
class FactContext:
    """What every fact of one webhook needs: the key, the provider, the event time, ``now``."""

    key: bytes
    provider: str
    event_at: datetime
    now: datetime

    def fact(self, kind: FactKind, object_id: str, occurred_at: datetime) -> PaymentFact:
        """An empty fact of ``kind`` for the provider object ``object_id``."""
        return PaymentFact(
            id=uuid4(),
            kind=kind,
            ref=ledger_ref(self.key, self.provider, kind, object_id),
            provider=self.provider,
            occurred_at=occurred_at,
            event_at=self.event_at,
            recorded_at=self.now,
            updated_at=self.now,
        )


def invoice_paid_fact(
    ctx: FactContext, invoice: ProviderInvoice, plan_key: str | None
) -> PaymentFact | None:
    """The fact of a paid invoice; None when nothing was paid (a trial, a 100 % coupon, an
    invoice settled from the credit balance)."""
    if invoice.status != "paid" or invoice.amount_paid <= 0:
        return None
    fact = ctx.fact(FactKind.INVOICE_PAID, invoice.id, invoice.paid_at or ctx.event_at)
    fact.currency = invoice.currency.lower()
    fact.amount = invoice.amount_paid
    fact.tax = invoice.tax
    fact.plan_key = plan_key
    fact.status = "paid"
    fact.period_start = invoice.period_start
    fact.period_end = invoice.period_end
    return fact


def payment_failed_fact(
    ctx: FactContext, invoice: ProviderInvoice, plan_key: str | None
) -> PaymentFact:
    """The fact of one failed attempt to collect an invoice: each attempt is its own fact (the
    provider retries the same invoice), a repeated event of one attempt is the same fact."""
    attempt = max(invoice.attempt_count, 1)
    fact = ctx.fact(FactKind.PAYMENT_FAILED, f"{invoice.id}#{attempt}", ctx.event_at)
    fact.currency = invoice.currency.lower()
    fact.amount = max(invoice.amount_due - invoice.amount_paid, 0)
    fact.plan_key = plan_key
    fact.status = invoice.status
    fact.attempt = attempt
    fact.period_start = invoice.period_start
    fact.period_end = invoice.period_end
    return fact


def refund_fact(ctx: FactContext, refund: ProviderRefund) -> PaymentFact:
    """The fact of a refund at the provider; the reason comes from the metadata an account
    erasure puts on its refunds."""
    fact = ctx.fact(FactKind.REFUND, refund.id, refund.created or ctx.event_at)
    fact.currency = refund.currency.lower()
    fact.amount = refund.amount
    fact.status = refund.status.value
    erasure = is_erasure_metadata(refund.metadata)
    fact.reason = RefundReason.ERASURE.value if erasure else RefundReason.OTHER.value
    return fact


def manual_refund_fact(ctx: FactContext, refund: ManualRefund) -> PaymentFact | None:
    """The fact of a refund the operator paid back by hand (ADR 0082, section 5); None while it
    is open. Keyed by the record, which the sweep deletes 30 days after it was settled."""
    if refund.settled_at is None:
        return None
    fact = ctx.fact(FactKind.REFUND, f"manual:{refund.id}", refund.settled_at)
    fact.currency = refund.currency.lower()
    fact.amount = refund.amount
    fact.status = "succeeded"
    fact.reason = RefundReason.ERASURE.value
    return fact


def dispute_fact(ctx: FactContext, dispute: ProviderDispute) -> PaymentFact:
    """The fact of a dispute in its current status; closed once the status says so."""
    fact = ctx.fact(FactKind.DISPUTE, dispute.id, dispute.created)
    fact.currency = dispute.currency.lower()
    fact.amount = dispute.amount
    fact.status = dispute.status
    fact.reason = dispute.reason
    fact.due_by = dispute.due_by
    fact.closed_at = ctx.event_at if dispute.status in CLOSED_DISPUTE_STATUSES else None
    return fact


def subscription_facts(
    ctx: FactContext, subscription: ProviderSubscription, plan_key: str | None
) -> list[PaymentFact]:
    """The start of a subscription once it has started, and its end once it is canceled. Each
    is written once per subscription; later events find them by their keyed hash."""
    facts: list[PaymentFact] = []
    if subscription.status in STARTED_STATUSES:
        started = ctx.fact(
            FactKind.SUBSCRIPTION_STARTED,
            subscription.id,
            subscription.started_at or ctx.event_at,
        )
        started.plan_key = plan_key
        started.status = subscription.status.value
        facts.append(started)
    if subscription.status is SubscriptionStatus.CANCELED:
        ended = ctx.fact(
            FactKind.SUBSCRIPTION_ENDED,
            subscription.id,
            subscription.ended_at or subscription.canceled_at or ctx.event_at,
        )
        ended.plan_key = plan_key
        ended.status = subscription.status.value
        facts.append(ended)
    return facts
