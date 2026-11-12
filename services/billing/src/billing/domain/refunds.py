# SPDX-FileCopyrightText: 2026 Lukas Körber
# SPDX-License-Identifier: AGPL-3.0-only

"""The refund of an erased account (ADR 0082, section 5), free of storage and provider.

When an account with a paid subscription is erased, Billing pays back the part of the current
period that was paid and is no longer used, then ends the subscription and deletes the customer
at the provider. The rule is applied to **every paid invoice of the subscription that bills
time containing the moment of the erasure** (:func:`covers`): the invoice of the period, and an
invoice of an upgrade within it that was billed on its own. Each is refunded against its own
payment:

    refund = (paid on the invoice for time after now - refunded already) x unused part

* **Paid for the time** is what the customer actually paid on the invoice (after discounts,
  coupons and the credit balance, with the taxes), shared among the invoice's **positive**
  lines in proportion to their amounts, and of that the share of the **recurring and proration
  lines whose period contains now**. A negative line (the credit for the unused time of the old
  plan in an upgrade) is not a line to share among: it has already lowered what was paid on its
  invoice, and the old plan's own invoice is refunded for its unused time separately, so
  sharing the paid amount among the positive lines refunds exactly the unused time of the new
  plan (the test of an upgrade proves it). One-off items are positive lines too: they get their
  share of what was paid, which is not refunded.
* **Metered usage is not refunded, by rule:** it bills consumption that has already happened
  (for the period before, in arrears), so none of it is unused. Its period is over, and it is
  passed over like any other line whose period does not contain now.
* **Refunded already** is every refund on that payment that is not failed or canceled.
* **The unused part** is time based, per line: ``(period_end - now) / (period_end -
  period_start)`` over the line's own period.
* The result is rounded **down** to the currency's smallest unit and is never more than what is
  left of the payment. Amounts are in the provider's smallest unit (cents; whole units for a
  zero-decimal currency such as JPY, so rounding down to an integer is right for both). Stripe
  writes ISK and UGX with two decimals but refunds them in whole units only: those are rounded
  down to a multiple of 100.
* It is **0** for a trial or a 100 % coupon (nothing paid), for an invoice paid without a
  payment at the provider (by the credit balance alone, or marked paid outside it), and once
  the period is over.

**Retries, per invoice.** A refund carries the metadata ``reason=account_erasure`` with the
account, the subscription and the invoice. An earlier refund of the same erasure and invoice
that is not failed or canceled means that invoice is done (whatever the amount has become
since); a failed one is counted, and the next refund is made under a new idempotency key. After
:data:`MAX_FAILED_REFUNDS` failed refunds, and at once when the provider refuses for good
(:class:`~billing.domain.errors.RefundImpossible`), the refund is **skipped**: the erasure goes
on, and the amount is recorded for the operator to pay out by hand (:class:`ManualRefund`).
"""

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import StrEnum
from fractions import Fraction
from math import floor
from uuid import UUID, uuid4

from billing.domain.models import (
    InvoiceLine,
    LineKind,
    ManualRefund,
    PaidInvoice,
    ProviderRefund,
    RefundStatus,
)

ERASURE_REASON = "account_erasure"
"""The ``reason`` in the metadata of every refund an account erasure makes."""

MAX_FAILED_REFUNDS = 3
"""Failed or canceled refunds of one erasure on one invoice after which the refund is skipped
(and recorded for the operator) rather than tried again."""

INITIATED = frozenset({RefundStatus.PENDING, RefundStatus.REQUIRES_ACTION, RefundStatus.SUCCEEDED})
"""A refund in one of these is on its way: the provider accepted it and moves the money without
anything more from MowDeck. ``pending`` counts: a refund to a bank account or some wallets stays
pending for days, and the erasure must not wait for it. ``requires_action`` counts too (the
provider asks the customer for the details it needs), with a warning to the operator."""

REFUNDABLE_LINES = frozenset({LineKind.RECURRING, LineKind.PRORATION})
"""Lines whose unused time is refunded: a subscription price, and a proration of a change
within the period (over its own period)."""

LONGEST_PERIOD = timedelta(days=366)
"""The longest billing period MowDeck sells (a year, a leap year included). An invoice that
bills time containing now was created at most this long ago, so a provider looks no further
back for the paid invoices of a subscription (:func:`earliest_invoice`)."""

SMALLEST_STEP = {"isk": 100, "ugx": 100}
"""Currencies whose amounts the provider writes with two decimals but refunds in whole units
(Stripe's special cases): a refund is a multiple of this step. Every other currency: 1."""

MANUAL_REFUND_RETENTION = timedelta(days=30)
"""How long a settled manual refund is kept before it is deleted (open ones are kept until
they are settled)."""


def unused_fraction(period_start: datetime, period_end: datetime, now: datetime) -> Fraction:
    """The part of a period that lies after ``now``: 1 before it starts, 0 once it is over."""
    total = period_end - period_start
    if total <= timedelta(0) or now >= period_end:
        return Fraction(0)
    if now <= period_start:
        return Fraction(1)
    return Fraction(_microseconds(period_end - now), _microseconds(total))


def _microseconds(delta: timedelta) -> int:
    return (delta.days * 86_400 + delta.seconds) * 1_000_000 + delta.microseconds


def earliest_invoice(now: datetime) -> datetime:
    """How far back the paid invoices of a subscription are looked for."""
    return now - LONGEST_PERIOD


def _refundable(line: InvoiceLine, now: datetime) -> bool:
    return (
        line.kind in REFUNDABLE_LINES
        and line.amount > 0
        and line.period_start <= now < line.period_end
    )


def covers(invoice: PaidInvoice, now: datetime) -> bool:
    """Whether the invoice bills time that contains ``now`` (a positive recurring or proration
    line whose period contains it): the invoices the refund of an erasure looks at."""
    return any(_refundable(line, now) for line in invoice.lines)


def refunded_already(refunds: Sequence[ProviderRefund]) -> int:
    """What the refunds of a payment took back, failed and canceled ones left out."""
    return sum(r.amount for r in refunds if r.status in INITIATED)


def round_down(amount: Fraction | int, currency: str) -> int:
    """Rounded down to the smallest unit the provider refunds in ``currency``."""
    whole = floor(amount)
    step = SMALLEST_STEP.get(currency.lower(), 1)
    return max(0, whole - whole % step)


def refund_amount(
    invoice: PaidInvoice | None, refunds: Sequence[ProviderRefund], now: datetime
) -> int:
    """The refund of the unused time paid on one invoice (the rule of the module), in the
    invoice's currency and smallest unit. ``refunds`` are the refunds of the invoice's payment."""
    if (
        invoice is None
        or invoice.status != "paid"
        or invoice.amount_paid <= 0
        or invoice.payment_ref is None
    ):
        return 0
    positive_total = sum(line.amount for line in invoice.lines if line.amount > 0)
    if positive_total <= 0:
        return 0
    paid = Fraction(0)
    unused = Fraction(0)
    for line in invoice.lines:
        if not _refundable(line, now):
            continue  # metered usage, one-off items, credits, or a period that is over
        share = Fraction(invoice.amount_paid * line.amount, positive_total)
        paid += share
        unused += share * unused_fraction(line.period_start, line.period_end, now)
    if paid <= 0:
        return 0
    already = refunded_already(refunds)
    owed = max(Fraction(0), paid - already) * (unused / paid)
    refundable = min(invoice.payment_amount, invoice.amount_paid) - already
    return round_down(min(owed, Fraction(max(0, refundable))), invoice.currency)


def erasure_metadata(account_id: UUID, subscription_id: str, invoice_id: str) -> dict[str, str]:
    """The metadata of a refund made by an account erasure: how a retry finds it again."""
    return {
        "reason": ERASURE_REASON,
        "account_id": str(account_id),
        "subscription_id": subscription_id,
        "invoice_id": invoice_id,
    }


def is_erasure_metadata(metadata: dict[str, str]) -> bool:
    """Whether a refund with this metadata was made by an account erasure."""
    return metadata.get("reason") == ERASURE_REASON


def is_erasure_refund(
    refund: ProviderRefund, account_id: UUID, subscription_id: str, invoice_id: str
) -> bool:
    """Whether ``refund`` was made by the erasure of this account for this invoice."""
    return refund.metadata == erasure_metadata(account_id, subscription_id, invoice_id)


def erasure_refund_lost(refund: ProviderRefund, refunds: Sequence[ProviderRefund]) -> bool:
    """A refund of an account erasure that failed or was canceled after the provider accepted
    it, with no other refund of the same erasure and invoice on its way (``refunds``: the
    refunds of its payment). The money did not go back and the erasure, finished by then, will
    not try again: the operator pays it out by hand. A failed refund that a later attempt of the
    erasure replaced is not lost."""
    if not is_erasure_metadata(refund.metadata) or refund.status in INITIATED:
        return False
    return not any(
        other.id != refund.id and other.metadata == refund.metadata and other.status in INITIATED
        for other in refunds
    )


def idempotency_key(account_id: UUID, subscription_id: str, invoice_id: str, attempt: int) -> str:
    """The idempotency key of the refund: the same for every retry of one attempt, a new one
    only after a refund failed (a provider answers a reused key with the failed refund)."""
    key = f"mowdeck-erasure-refund-{account_id}-{subscription_id}-{invoice_id}"
    return key if attempt == 0 else f"{key}-{attempt}"


class RefundAction(StrEnum):
    """What the erasure does about the refund of one invoice."""

    NONE = "none"
    """Nothing to refund (nothing paid for the rest of the period)."""
    DONE = "done"
    """An earlier attempt of this erasure made the refund; it is on its way."""
    CREATE = "create"
    """Make the refund."""
    SKIP = "skip"
    """The refund cannot be made: the erasure goes on and the operator pays it out by hand."""


@dataclass(frozen=True, slots=True)
class RefundPlan:
    """The decision for one invoice, with what the provider call needs."""

    action: RefundAction
    amount: int = 0
    currency: str = ""
    payment_ref: str | None = None
    invoice_id: str | None = None
    idempotency_key: str = ""
    metadata: dict[str, str] = field(default_factory=dict)
    existing: ProviderRefund | None = None
    reason: str = ""


def plan_refund(
    account_id: UUID,
    invoice: PaidInvoice | None,
    refunds: Sequence[ProviderRefund],
    now: datetime,
) -> RefundPlan:
    """Decide the refund of one invoice of an erased account (the rule of the module)."""
    if invoice is None:
        return RefundPlan(RefundAction.NONE, reason="no paid invoice")
    sub, inv = invoice.subscription_id, invoice.invoice_id
    ours = [r for r in refunds if is_erasure_refund(r, account_id, sub, inv)]
    made = [r for r in ours if r.status in INITIATED]
    if made:
        return RefundPlan(
            RefundAction.DONE,
            amount=made[0].amount,
            currency=made[0].currency,
            payment_ref=invoice.payment_ref,
            invoice_id=inv,
            existing=made[0],
            reason="refunded by an earlier attempt",
        )
    amount = refund_amount(invoice, refunds, now)
    if amount == 0:
        return RefundPlan(
            RefundAction.NONE, currency=invoice.currency, invoice_id=inv, reason="nothing to refund"
        )
    failed = len(ours)
    plan = RefundPlan(
        RefundAction.CREATE,
        amount=amount,
        currency=invoice.currency,
        payment_ref=invoice.payment_ref,
        invoice_id=inv,
        idempotency_key=idempotency_key(account_id, sub, inv, failed),
        metadata=erasure_metadata(account_id, sub, inv),
    )
    if failed >= MAX_FAILED_REFUNDS:
        return skipped(plan, f"{failed} refunds failed")
    return plan


def skipped(plan: RefundPlan, reason: str) -> RefundPlan:
    """The refund given up for good: after too many failed refunds, or when the provider
    refuses it for a reason that does not pass (``RefundImpossible``). The erasure goes on and
    the amount is recorded for the operator (:func:`manual_refund`)."""
    return RefundPlan(
        RefundAction.SKIP,
        amount=plan.amount,
        currency=plan.currency,
        payment_ref=plan.payment_ref,
        invoice_id=plan.invoice_id,
        metadata=plan.metadata,
        reason=reason,
    )


def is_initiated(refund: ProviderRefund) -> bool:
    """Whether the provider accepted the refund (see :data:`INITIATED`)."""
    return refund.status in INITIATED


# --- refunds the operator pays by hand ---------------------------------------------------------


def manual_refund_key(payment_ref: str, invoice_ref: str | None) -> str:
    """One record per invoice and payment, whichever path wrote it first: the erasure that
    skipped the refund or the webhook that saw it fail later. So the operator is never asked to
    pay one refund twice."""
    return f"{payment_ref}:{invoice_ref or ''}"


def manual_refund(plan: RefundPlan, now: datetime) -> ManualRefund:
    """The record of a skipped refund (``plan.action`` is ``SKIP``). It names the payment and
    the invoice, never the account: the payment is what the operator finds in the provider's
    dashboard and pays back."""
    payment = plan.payment_ref or ""
    return ManualRefund(
        id=uuid4(),
        key=manual_refund_key(payment, plan.invoice_id),
        payment_ref=payment,
        invoice_ref=plan.invoice_id,
        amount=plan.amount,
        currency=plan.currency,
        reason=plan.reason,
        created_at=now,
    )


def lost_refund_record(refund: ProviderRefund, now: datetime) -> ManualRefund:
    """The record of a refund of an erasure that failed after the erasure was over."""
    payment = refund.payment_ref or refund.id
    invoice = refund.metadata.get("invoice_id") or None
    reason = f"refund {refund.id} {refund.status.value}"
    if refund.failure_reason:
        reason = f"{reason} ({refund.failure_reason})"
    return ManualRefund(
        id=uuid4(),
        key=manual_refund_key(payment, invoice),
        payment_ref=payment,
        invoice_ref=invoice,
        amount=refund.amount,
        currency=refund.currency,
        reason=reason,
        created_at=now,
    )


def settled_before(now: datetime) -> datetime:
    """Settled records older than this are deleted (:data:`MANUAL_REFUND_RETENTION`)."""
    return now - MANUAL_REFUND_RETENTION
