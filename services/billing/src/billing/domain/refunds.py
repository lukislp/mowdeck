# SPDX-FileCopyrightText: 2026 Lukas Körber
# SPDX-License-Identifier: AGPL-3.0-only

"""The refund of an erased account (ADR 0082, section 5), free of storage and provider.

When an account with a paid subscription is erased, Billing pays back the part of the current
period that was paid and is no longer used, then ends the subscription and deletes the customer
at the provider. The rule, for one subscription:

    refund = (paid for the current period - refunded already) x unused part of the period

* **Paid for the current period** is taken from the subscription's latest paid invoice: what
  the customer actually paid on it (after discounts, coupons and the credit balance, with the
  taxes), shared among the invoice's lines in proportion to their amounts, and of that only the
  share of the **recurring** lines whose period contains the moment of the erasure. Not
  refunded: metered usage (it is billed in arrears, for the period before, so its period has
  ended), prorations of a change within the period and one-off items.
* **Refunded already** is every refund on that payment that is not failed or canceled.
* **The unused part** is time based: ``(period_end - now) / (period_end - period_start)``.
* The result is rounded **down** to the currency's smallest unit and is never more than what is
  left of the payment. Amounts are in the provider's smallest unit (cents; whole units for a
  zero-decimal currency such as JPY, so rounding down to an integer is right for both). Stripe
  writes ISK and UGX with two decimals but refunds them in whole units only: those are rounded
  down to a multiple of 100.
* It is **0** without a paid invoice, for a trial or a 100 % coupon (nothing paid), for an
  invoice paid without a payment at the provider (by the credit balance alone, or marked paid
  outside it), and once the period is over.

**Retries.** A refund carries the metadata ``reason=account_erasure`` with the account, the
subscription and the invoice. An earlier refund of the same erasure that is not failed or
canceled means the refund is done (whatever the amount has become since); a failed one is
counted, and the next refund is made under a new idempotency key. After
:data:`MAX_FAILED_REFUNDS` failed refunds, and at once when the provider refuses for good
(:class:`~billing.domain.errors.RefundImpossible`), the refund is **skipped**: the erasure goes
on and the operator is warned with the amount and the account, to pay it out by hand.
"""

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import StrEnum
from fractions import Fraction
from math import floor
from uuid import UUID

from billing.domain.models import LineKind, PaidInvoice, ProviderRefund, RefundStatus

ERASURE_REASON = "account_erasure"
"""The ``reason`` in the metadata of every refund an account erasure makes."""

MAX_FAILED_REFUNDS = 3
"""Failed or canceled refunds of one erasure on one payment after which the refund is skipped
(and the operator warned) rather than tried again."""

INITIATED = frozenset({RefundStatus.PENDING, RefundStatus.REQUIRES_ACTION, RefundStatus.SUCCEEDED})
"""A refund in one of these is on its way: the provider accepted it and moves the money without
anything more from MowDeck. ``pending`` counts: a refund to a bank account or some wallets stays
pending for days, and the erasure must not wait for it. ``requires_action`` counts too (the
provider asks the customer for the details it needs), with a warning to the operator."""

SMALLEST_STEP = {"isk": 100, "ugx": 100}
"""Currencies whose amounts the provider writes with two decimals but refunds in whole units
(Stripe's special cases): a refund is a multiple of this step. Every other currency: 1."""


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
    """The refund of the unused part of the paid period (the rule of the module), in the
    invoice's currency and smallest unit. ``refunds`` are the refunds of the invoice's payment."""
    if (
        invoice is None
        or invoice.status != "paid"
        or invoice.amount_paid <= 0
        or invoice.payment_ref is None
    ):
        return 0
    lines_total = sum(line.amount for line in invoice.lines)
    if lines_total <= 0:
        return 0
    paid = Fraction(0)
    unused = Fraction(0)
    for line in invoice.lines:
        if line.kind is not LineKind.RECURRING or line.amount <= 0:
            continue
        if not line.period_start <= now < line.period_end:
            continue  # metered usage of the period before, or a period that is over
        share = Fraction(invoice.amount_paid * line.amount, lines_total)
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


def erasure_refund_lost(refund: ProviderRefund) -> bool:
    """A refund of an account erasure that failed or was canceled after the provider accepted
    it: the money did not go back, and the erasure, finished by then, will not try again. The
    operator pays it out by hand."""
    return is_erasure_metadata(refund.metadata) and refund.status not in INITIATED


def is_erasure_refund(
    refund: ProviderRefund, account_id: UUID, subscription_id: str, invoice_id: str
) -> bool:
    """Whether ``refund`` was made by the erasure of this account for this invoice."""
    return refund.metadata == erasure_metadata(account_id, subscription_id, invoice_id)


def idempotency_key(account_id: UUID, subscription_id: str, invoice_id: str, attempt: int) -> str:
    """The idempotency key of the refund: the same for every retry of one attempt, a new one
    only after a refund failed (a provider answers a reused key with the failed refund)."""
    key = f"mowdeck-erasure-refund-{account_id}-{subscription_id}-{invoice_id}"
    return key if attempt == 0 else f"{key}-{attempt}"


class RefundAction(StrEnum):
    """What the erasure does about the refund of one subscription."""

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
    """The decision for one subscription, with what the provider call needs."""

    action: RefundAction
    amount: int = 0
    currency: str = ""
    payment_ref: str | None = None
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
    """Decide the refund of one subscription of an erased account (the rule of the module)."""
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
            existing=made[0],
            reason="refunded by an earlier attempt",
        )
    amount = refund_amount(invoice, refunds, now)
    if amount == 0:
        return RefundPlan(RefundAction.NONE, currency=invoice.currency, reason="nothing to refund")
    failed = len(ours)
    plan = RefundPlan(
        RefundAction.CREATE,
        amount=amount,
        currency=invoice.currency,
        payment_ref=invoice.payment_ref,
        idempotency_key=idempotency_key(account_id, sub, inv, failed),
        metadata=erasure_metadata(account_id, sub, inv),
    )
    if failed >= MAX_FAILED_REFUNDS:
        return skipped(plan, f"{failed} refunds failed")
    return plan


def skipped(plan: RefundPlan, reason: str) -> RefundPlan:
    """The refund given up for good: after too many failed refunds, or when the provider
    refuses it for a reason that does not pass (``RefundImpossible``). The erasure goes on and
    the operator is warned with the amount, so that it can be paid out by hand."""
    return RefundPlan(
        RefundAction.SKIP,
        amount=plan.amount,
        currency=plan.currency,
        payment_ref=plan.payment_ref,
        metadata=plan.metadata,
        reason=reason,
    )


def is_initiated(refund: ProviderRefund) -> bool:
    """Whether the provider accepted the refund (see :data:`INITIATED`)."""
    return refund.status in INITIATED
