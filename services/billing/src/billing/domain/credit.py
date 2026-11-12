# SPDX-FileCopyrightText: 2026 Lukas Körber
# SPDX-License-Identifier: AGPL-3.0-only

"""The credit balance of an erased account is paid back (ADR 0082, section 5), free of storage
and provider.

A downgrade with proration, or a credit the operator gave, can leave a **credit balance** on
the customer at the provider: money the customer paid and has not used. Deleting the customer
would lose it, so the erasure pays it back first. It reads the balance **after** the refunds of
the paid periods and after the subscriptions ended: ending a subscription without proration
(``prorate=false``, no final invoice) adds no credit, so the balance read then is the credit to
pay back.

* **The credit** is the balance in its currency when it is a credit (:class:`CreditBalance`,
  ``amount > 0``), plus what earlier attempts of this erasure took off it already
  (``cleared``). A zero balance, or one the customer owes (``amount <= 0``), means nothing to
  do: an erased customer is **never charged**, and a debt is dropped with the customer.
* **Spread over the most recent payments, newest first.** Each payment in the balance's
  currency takes at most what is left of it: its amount minus what was refunded of it already,
  the refunds of this erasure's paid periods included (the larger of the provider's own figure
  and the refunds on their way, :func:`~billing.domain.refunds.refunded_already`). Rounded down
  to the currency's refund step (:func:`~billing.domain.refunds.round_down`). The payments are
  those of the last :data:`~billing.domain.refunds.LONGEST_PERIOD` (:func:`earliest_payment`).
* **What no payment can take** (none in the window, all refunded, or a balance in another
  currency than every payment) is recorded for the operator to pay out by hand, in full when the
  currencies differ (:func:`credit_records`).
* Then the balance is **zeroed** with a balance transaction of ``amount`` (skipped when it is
  0 already), and the customer is deleted.

**Retries.** A refund of the credit carries the metadata ``reason=account_erasure_credit_balance``
with the account and the payment, under the key ``mowdeck-erasure-credit-<account>-<payment>``.
A refund of this erasure on that payment that is on its way means the payment is done (its
amount counts as paid back); failed ones are counted and the next one is made under a new key
(suffix ``-1``, ``-2``), and after :data:`~billing.domain.refunds.MAX_FAILED_REFUNDS` the
refund is skipped and recorded, as for a paid period. The balance transaction carries the
erasure's metadata too, so a retry after the balance was zeroed reads it back as ``cleared``
and plans the same refunds again (finding them done).

Example: a credit of 25.00 EUR; the newest payment was 9.00 of which this erasure refunded 6.00
for the unused period (3.00 left), the one before 9.00 with 1.00 refunded in the dashboard
(8.00 left), the oldest 9.00 (9.00 left): refunds of 3.00, 8.00 and 9.00, and 5.00 recorded for
the operator; then the balance is zeroed with 25.00.
"""

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from uuid import UUID, uuid4

from billing.domain.models import CreditBalance, ManualRefund, ProviderRefund, RecentPayment
from billing.domain.refunds import (
    CREDIT_REASON,
    INITIATED,
    LONGEST_PERIOD,
    MAX_FAILED_REFUNDS,
    RefundAction,
    RefundPlan,
    earliest_invoice,
    manual_refund,
    refunded_already,
    round_down,
    skipped,
)

CREDIT_PREFIX = "credit balance: "
"""The start of the reason of every manual refund record of a credit balance."""


def earliest_payment(now: datetime) -> datetime:
    """How far back the payments a credit balance is paid back against are looked for: as far
    as the paid invoices of a period (one longest billing period)."""
    return earliest_invoice(now)


def credit_owed(balance: CreditBalance | None) -> int:
    """The credit the erasure pays back: the open credit plus what earlier attempts cleared
    already; 0 without a customer, without a credit and when the customer owes money."""
    if balance is None:
        return 0
    return max(balance.amount, 0) + max(balance.cleared, 0)


def credit_metadata(account_id: UUID, payment_ref: str) -> dict[str, str]:
    """The metadata of a refund that pays back the credit balance against one payment: how a
    retry finds it again."""
    return {"reason": CREDIT_REASON, "account_id": str(account_id), "payment": payment_ref}


def clearing_metadata(account_id: UUID) -> dict[str, str]:
    """The metadata of the balance transaction that zeroes the credit balance."""
    return {"reason": CREDIT_REASON, "account_id": str(account_id)}


def is_clearing_metadata(metadata: dict[str, str]) -> bool:
    """Whether a balance transaction with this metadata zeroed a credit balance on an erasure
    (what a provider sums up as :attr:`CreditBalance.cleared`)."""
    return metadata.get("reason") == CREDIT_REASON


def credit_idempotency_key(account_id: UUID, payment_ref: str, attempt: int) -> str:
    """The idempotency key of a refund of the credit against one payment: the same for every
    retry of one attempt, a new one only after a refund failed."""
    key = f"mowdeck-erasure-credit-{account_id}-{payment_ref}"
    return key if attempt == 0 else f"{key}-{attempt}"


def clearing_idempotency_key(account_id: UUID, customer_id: str, amount: int, currency: str) -> str:
    """The idempotency key of the balance transaction that zeroes the balance. It names the
    amount: a retry of the same zeroing reuses it, a balance that changed meanwhile gets a new
    one (a provider refuses a key reused with other parameters)."""
    return f"mowdeck-erasure-credit-clear-{account_id}-{customer_id}-{amount}{currency.lower()}"


def credit_leftover_key(customer_id: str) -> str:
    """The key of the record of the credit no payment could take: one per customer."""
    return f"{customer_id}:credit-balance"


@dataclass(frozen=True, slots=True)
class CreditPlan:
    """What the erasure does about the credit balance of one customer."""

    customer_id: str
    credit: int = 0
    """The credit to pay back (0: nothing to do)."""
    currency: str = ""
    refunds: tuple[RefundPlan, ...] = ()
    """One plan per payment that takes a part, newest first (``CREATE``, ``DONE`` or ``SKIP``)."""
    leftover: int = 0
    """The part no payment can take: recorded for the operator."""
    leftover_reason: str = ""
    leftover_ref: str = ""
    """What the operator finds the payer by: the newest payment, else the customer."""
    clear: int = 0
    """What the balance transaction takes off the balance now (0: skipped)."""
    clear_key: str = ""
    clear_metadata: dict[str, str] = field(default_factory=dict)
    reason: str = ""


def plan_credit_refunds(
    account_id: UUID,
    customer_id: str,
    balance: CreditBalance | None,
    payments: Sequence[tuple[RecentPayment, Sequence[ProviderRefund]]],
) -> CreditPlan:
    """Decide how the credit balance of an erased account's customer is paid back (the rule of
    the module). ``payments`` are the customer's recent successful payments, each with the
    refunds of it, in any order."""
    if balance is None:
        return CreditPlan(customer_id, reason="no customer at the provider")
    open_credit = max(balance.amount, 0)
    credit = credit_owed(balance)
    if credit <= 0:
        reason = "the customer owes money" if balance.amount < 0 else "no credit balance"
        return CreditPlan(customer_id, currency=balance.currency, reason=reason)
    currency = balance.currency.lower()
    newest = sorted(payments, key=lambda item: item[0].created, reverse=True)
    usable = [item for item in newest if item[0].currency.lower() == currency]
    remaining = credit
    plans: list[RefundPlan] = []
    for payment, refunds in usable:
        if remaining <= 0:
            break
        plan = _plan_payment(account_id, payment, refunds, remaining, currency)
        if plan is not None:
            plans.append(plan)
            remaining -= plan.amount
    leftover = max(remaining, 0)
    if newest and not usable:
        others = sorted({p.currency.lower() for p, _ in newest})
        leftover_reason = f"the credit is in {currency}, the payments in {', '.join(others)}"
    elif not newest:
        leftover_reason = f"no payment of the last {LONGEST_PERIOD.days} days to refund it to"
    else:
        leftover_reason = "the recent payments are refunded in full"
    return CreditPlan(
        customer_id,
        credit=credit,
        currency=currency,
        refunds=tuple(plans),
        leftover=leftover,
        leftover_reason=f"{CREDIT_PREFIX}{leftover_reason}" if leftover else "",
        leftover_ref=newest[0][0].payment_ref if newest else customer_id,
        clear=open_credit,
        clear_key=clearing_idempotency_key(account_id, customer_id, open_credit, currency)
        if open_credit
        else "",
        clear_metadata=clearing_metadata(account_id),
    )


def _plan_payment(
    account_id: UUID,
    payment: RecentPayment,
    refunds: Sequence[ProviderRefund],
    remaining: int,
    currency: str,
) -> RefundPlan | None:
    """The part of the credit one payment takes; None when it can take nothing."""
    metadata = credit_metadata(account_id, payment.payment_ref)
    ours = [r for r in refunds if r.metadata == metadata]
    made = [r for r in ours if r.status in INITIATED]
    if made:
        return RefundPlan(
            RefundAction.DONE,
            amount=sum(r.amount for r in made),
            currency=currency,
            payment_ref=payment.payment_ref,
            existing=made[0],
            reason="refunded by an earlier attempt",
        )
    left = payment.amount - max(payment.refunded, refunded_already(refunds))
    amount = round_down(min(remaining, left), currency)
    if amount <= 0:
        return None
    failed = len(ours)
    plan = RefundPlan(
        RefundAction.CREATE,
        amount=amount,
        currency=currency,
        payment_ref=payment.payment_ref,
        idempotency_key=credit_idempotency_key(account_id, payment.payment_ref, failed),
        metadata=metadata,
    )
    if failed >= MAX_FAILED_REFUNDS:
        return skipped(plan, f"{CREDIT_PREFIX}{failed} refunds failed")
    return plan


def credit_skipped(plan: RefundPlan, reason: str) -> RefundPlan:
    """A refund of the credit the provider refused for good (``RefundImpossible``)."""
    return skipped(plan, f"{CREDIT_PREFIX}{reason}")


def credit_records(
    plan: CreditPlan, refunds: Sequence[RefundPlan], now: datetime
) -> list[ManualRefund]:
    """The records the operator pays out by hand for one credit balance: each skipped refund
    (``refunds``, the plans as the erasure carried them out) and the leftover. A skipped refund
    names its payment and no invoice (:func:`~billing.domain.refunds.manual_refund_key`); the
    leftover names the newest payment, or the customer when there is none, and has a key of its
    own (:func:`credit_leftover_key`)."""
    records = [manual_refund(p, now) for p in refunds if p.action is RefundAction.SKIP]
    if plan.leftover > 0:
        records.append(
            ManualRefund(
                id=uuid4(),
                key=credit_leftover_key(plan.customer_id),
                payment_ref=plan.leftover_ref,
                invoice_ref=None,
                amount=plan.leftover,
                currency=plan.currency,
                reason=plan.leftover_reason,
                created_at=now,
            )
        )
    return records
