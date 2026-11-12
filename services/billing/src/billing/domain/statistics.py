# SPDX-FileCopyrightText: 2026 Lukas Körber
# SPDX-License-Identifier: AGPL-3.0-only

"""The statistics of the operator dashboard (ADR 0083), computed from the payments ledger and
Billing's live subscription and invoice rows. Pure: the caller passes the rows and ``now``.

Rules:

* **Months** are local calendar months in the configured zone
  (``MOWDECK_BILLING_REPORT_TIMEZONE``, default ``Europe/Berlin``; :data:`DEFAULT_ZONE` when a
  caller names none), keyed ``YYYY-MM``, computed with ``zoneinfo`` so that daylight saving
  time never moves an instant into another month; a window of
  ``n`` months ends with the month of ``now``. A fact belongs to the month of its
  ``occurred_at``: a paid invoice to the month it was paid, a refund to the month it was
  created, a dispute to the month it was opened.
* **Money** is never added across currencies: every amount is grouped by currency, in the
  provider's smallest unit (see :mod:`billing.domain.currency`).
* **Net revenue** of a month is the gross (what paid invoices took, tax included) minus the
  refunds created in that month that take money back (``pending``, ``requires_action``,
  ``succeeded``; :data:`~billing.domain.ledger.EFFECTIVE_REFUND_STATUSES`). Disputes are shown
  on their own and not taken off.
* **MRR** (monthly recurring revenue) is the list price of every subscription that is billed
  now (``active`` or ``past_due``; a trial pays nothing yet) normalised to one month: a yearly
  price divided by 12, a price every ``n`` months divided by ``n``, a weekly price times 52/12,
  a daily one times 365/12. Exact fractions per currency, rounded half up to the smallest unit
  once at the end. Discounts and taxes are not in it (the price list, not the invoices). A
  subscription whose plan has no known price is counted as ``unpriced``.
* **Overdue**: MowDeck's subscriptions charge automatically, so an invoice is due when it is
  issued; an invoice still ``open`` after its due time is overdue.
"""

from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, tzinfo
from fractions import Fraction
from math import floor

from mowdeck_contracts.v1 import SubscriptionStatus

from billing.domain.ledger import (
    CLOSED_DISPUTE_STATUSES,
    EFFECTIVE_REFUND_STATUSES,
    FactKind,
    PaymentFact,
)
from billing.domain.models import PlanPrice

DEFAULT_ZONE: tzinfo = UTC
"""The zone of the month buckets when the caller names none (Billing passes
``MOWDECK_BILLING_REPORT_TIMEZONE``)."""

MRR_STATUSES = frozenset({SubscriptionStatus.ACTIVE, SubscriptionStatus.PAST_DUE})
"""Subscriptions that count towards the MRR: billed now."""

COUNTED_STATUSES = (
    SubscriptionStatus.TRIALING,
    SubscriptionStatus.ACTIVE,
    SubscriptionStatus.PAST_DUE,
)
"""The statuses the dashboard counts per plan."""

INTERVALS_PER_MONTH = {
    "month": Fraction(1),
    "year": Fraction(1, 12),
    "week": Fraction(52, 12),
    "day": Fraction(365, 12),
}
"""How many intervals of a price fall into one month (the MRR normalisation)."""


def month_of(at: datetime, zone: tzinfo = DEFAULT_ZONE) -> str:
    """The month bucket of an instant: ``YYYY-MM`` of its local date in ``zone``. The local
    date comes from the zone's rules for that instant (``zoneinfo``), so a summer and a winter
    offset are both right and a DST change never moves an instant into another month."""
    return at.astimezone(zone).strftime("%Y-%m")


def recent_months(now: datetime, count: int, zone: tzinfo = DEFAULT_ZONE) -> list[str]:
    """The ``count`` months up to and including the local month of ``now``, oldest first."""
    local = now.astimezone(zone)
    index = local.year * 12 + local.month - 1
    return [f"{i // 12:04d}-{i % 12 + 1:02d}" for i in range(index - count + 1, index + 1)]


def window_start(now: datetime, count: int, zone: tzinfo = DEFAULT_ZONE) -> datetime:
    """The first instant of the oldest month of :func:`recent_months`: local midnight of its
    first day in ``zone`` (no zone changes its offset at midnight on the first)."""
    first = recent_months(now, count, zone)[0]
    year, month = (int(part) for part in first.split("-"))
    return datetime(year, month, 1, tzinfo=zone)


def round_half_up(amount: Fraction) -> int:
    """An exact amount rounded half up to an integer (amounts are never negative here)."""
    return floor(amount + Fraction(1, 2))


# --- revenue -------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RevenueMonth:
    """One month of one currency."""

    month: str
    gross: int
    tax: int
    refunded: int
    invoices: int
    refunds: int

    @property
    def net(self) -> int:
        """Gross minus refunds (may be negative in a month with old refunds)."""
        return self.gross - self.refunded


@dataclass(frozen=True, slots=True)
class CurrencyRevenue:
    """The months of one currency, oldest first, with the totals of the window."""

    currency: str
    months: list[RevenueMonth]

    @property
    def gross(self) -> int:
        """Total gross."""
        return sum(m.gross for m in self.months)

    @property
    def refunded(self) -> int:
        """Total refunded."""
        return sum(m.refunded for m in self.months)

    @property
    def net(self) -> int:
        """Total net."""
        return self.gross - self.refunded


def _in_window(
    facts: Iterable[PaymentFact], kind: FactKind, months: Sequence[str], zone: tzinfo
) -> list[tuple[str, PaymentFact]]:
    wanted = set(months)
    found = []
    for fact in facts:
        if fact.kind is kind:
            month = month_of(fact.occurred_at, zone)
            if month in wanted:
                found.append((month, fact))
    return found


def is_effective_refund(fact: PaymentFact) -> bool:
    """A refund that takes money back (see :data:`EFFECTIVE_REFUND_STATUSES`)."""
    return fact.kind is FactKind.REFUND and fact.status in EFFECTIVE_REFUND_STATUSES


def revenue(
    facts: Iterable[PaymentFact], months: Sequence[str], zone: tzinfo = DEFAULT_ZONE
) -> list[CurrencyRevenue]:
    """Gross, tax, refunds and net per month and currency; every currency with a paid invoice
    or a refund in the window gets every month (zeros included), currencies sorted."""
    facts = list(facts)
    gross: dict[tuple[str, str], list[int]] = defaultdict(lambda: [0, 0, 0, 0, 0])
    for month, fact in _in_window(facts, FactKind.INVOICE_PAID, months, zone):
        row = gross[(fact.currency or "", month)]
        row[0] += fact.amount
        row[1] += fact.tax or 0
        row[3] += 1
    for month, fact in _in_window(facts, FactKind.REFUND, months, zone):
        if is_effective_refund(fact):
            row = gross[(fact.currency or "", month)]
            row[2] += fact.amount
            row[4] += 1
    currencies = sorted({currency for currency, _ in gross})
    return [
        CurrencyRevenue(
            currency,
            [
                RevenueMonth(month, *gross.get((currency, month), [0, 0, 0, 0, 0]))
                for month in months
            ],
        )
        for currency in currencies
    ]


# --- refunds -------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RefundMonth:
    """The refunds of one month, currency and reason that take money back."""

    month: str
    currency: str
    reason: str
    count: int
    amount: int


def refunds_by_month(
    facts: Iterable[PaymentFact], months: Sequence[str], zone: tzinfo = DEFAULT_ZONE
) -> list[RefundMonth]:
    """Sum and count per month, currency and reason; only groups with a refund, by month, then
    currency, then reason."""
    groups: dict[tuple[str, str, str], list[int]] = defaultdict(lambda: [0, 0])
    for month, fact in _in_window(facts, FactKind.REFUND, months, zone):
        if is_effective_refund(fact):
            group = groups[(month, fact.currency or "", fact.reason or "other")]
            group[0] += 1
            group[1] += fact.amount
    return [
        RefundMonth(month, currency, reason, count, amount)
        for (month, currency, reason), (count, amount) in sorted(groups.items())
    ]


# --- payment problems ----------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class FailureMonth:
    """Failed payment attempts of one month and currency."""

    month: str
    currency: str
    count: int
    amount: int


def failures_by_month(
    facts: Iterable[PaymentFact], months: Sequence[str], zone: tzinfo = DEFAULT_ZONE
) -> list[FailureMonth]:
    """Count and sum of failed attempts per month and currency, by month then currency."""
    groups: dict[tuple[str, str], list[int]] = defaultdict(lambda: [0, 0])
    for month, fact in _in_window(facts, FactKind.PAYMENT_FAILED, months, zone):
        group = groups[(month, fact.currency or "")]
        group[0] += 1
        group[1] += fact.amount
    return [
        FailureMonth(month, currency, count, amount)
        for (month, currency), (count, amount) in sorted(groups.items())
    ]


def latest(facts: Iterable[PaymentFact], kind: FactKind, limit: int) -> list[PaymentFact]:
    """The newest facts of a kind first (by ``occurred_at``), at most ``limit``."""
    found = [fact for fact in facts if fact.kind is kind]
    found.sort(key=lambda fact: (fact.occurred_at, str(fact.id)), reverse=True)
    return found[:limit]


@dataclass(frozen=True, slots=True)
class OpenInvoice:
    """An invoice still open, as much as the statistics need of it: no account, no number."""

    currency: str
    amount_due: int
    due_at: datetime


@dataclass(frozen=True, slots=True)
class InvoiceBacklog:
    """The open invoices of one currency; ``overdue`` ones are open after their due time."""

    currency: str
    open_count: int
    open_amount: int
    overdue_count: int
    overdue_amount: int


def invoice_backlog(invoices: Iterable[OpenInvoice], now: datetime) -> list[InvoiceBacklog]:
    """Open and overdue invoices per currency (sorted)."""
    groups: dict[str, list[int]] = defaultdict(lambda: [0, 0, 0, 0])
    for invoice in invoices:
        group = groups[invoice.currency.lower()]
        group[0] += 1
        group[1] += invoice.amount_due
        if invoice.due_at < now:
            group[2] += 1
            group[3] += invoice.amount_due
    return [InvoiceBacklog(currency, *groups[currency]) for currency in sorted(groups)]


@dataclass(frozen=True, slots=True)
class DisputeMonth:
    """The disputes opened in one month and currency, and how many of them were lost."""

    month: str
    currency: str
    opened: int
    amount: int
    lost: int


def disputes_by_month(
    facts: Iterable[PaymentFact], months: Sequence[str], zone: tzinfo = DEFAULT_ZONE
) -> list[DisputeMonth]:
    """Disputes per month opened and currency, by month then currency."""
    groups: dict[tuple[str, str], list[int]] = defaultdict(lambda: [0, 0, 0])
    for month, fact in _in_window(facts, FactKind.DISPUTE, months, zone):
        group = groups[(month, fact.currency or "")]
        group[0] += 1
        group[1] += fact.amount
        group[2] += int(fact.status == "lost")
    return [
        DisputeMonth(month, currency, *values)
        for (month, currency), values in sorted(groups.items())
    ]


def is_open_dispute(fact: PaymentFact) -> bool:
    """A dispute that is not decided yet."""
    return fact.kind is FactKind.DISPUTE and fact.status not in CLOSED_DISPUTE_STATUSES


def open_disputes(facts: Iterable[PaymentFact]) -> list[PaymentFact]:
    """Undecided disputes, the one whose evidence is due first on top (no due date last)."""
    found = [fact for fact in facts if is_open_dispute(fact)]
    far = datetime.max.replace(tzinfo=UTC)
    found.sort(key=lambda fact: (fact.due_by or far, fact.occurred_at, str(fact.id)))
    return found


def closed_disputes(facts: Iterable[PaymentFact], limit: int) -> list[PaymentFact]:
    """Decided disputes, the most recently closed first, at most ``limit``."""
    found = [f for f in facts if f.kind is FactKind.DISPUTE and not is_open_dispute(f)]
    found.sort(key=lambda f: (f.closed_at or f.occurred_at, str(f.id)), reverse=True)
    return found[:limit]


# --- subscriptions -------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class FlowMonth:
    """Subscriptions started and ended in one month."""

    month: str
    started: int
    ended: int


def subscription_flow(
    facts: Iterable[PaymentFact], months: Sequence[str], zone: tzinfo = DEFAULT_ZONE
) -> list[FlowMonth]:
    """New and ended subscriptions per month (every month of the window)."""
    facts = list(facts)
    started: dict[str, int] = defaultdict(int)
    ended: dict[str, int] = defaultdict(int)
    for month, _ in _in_window(facts, FactKind.SUBSCRIPTION_STARTED, months, zone):
        started[month] += 1
    for month, _ in _in_window(facts, FactKind.SUBSCRIPTION_ENDED, months, zone):
        ended[month] += 1
    return [FlowMonth(month, started[month], ended[month]) for month in months]


@dataclass(frozen=True, slots=True)
class SubscriptionTally:
    """How many subscriptions of a plan are in a status (Billing's live rows)."""

    plan_key: str | None
    status: SubscriptionStatus
    count: int


@dataclass(frozen=True, slots=True)
class PlanCount:
    """The counted subscriptions of one plan."""

    plan_key: str | None
    trialing: int
    active: int
    past_due: int

    @property
    def total(self) -> int:
        """All counted statuses together."""
        return self.trialing + self.active + self.past_due


def plan_counts(tallies: Iterable[SubscriptionTally]) -> list[PlanCount]:
    """Trialing, active and past-due subscriptions per plan; plans without any left out,
    sorted by plan key (an unknown plan last)."""
    counts: dict[str | None, dict[SubscriptionStatus, int]] = defaultdict(lambda: defaultdict(int))
    for tally in tallies:
        if tally.status in COUNTED_STATUSES:
            counts[tally.plan_key][tally.status] += tally.count
    return [
        PlanCount(
            plan,
            counts[plan][SubscriptionStatus.TRIALING],
            counts[plan][SubscriptionStatus.ACTIVE],
            counts[plan][SubscriptionStatus.PAST_DUE],
        )
        for plan in sorted(counts, key=lambda key: (key is None, key or ""))
    ]


def monthly_amount(price: PlanPrice) -> Fraction | None:
    """A price normalised to one month (see the module rules); None when its amount or its
    interval is not known."""
    if price.unit_amount is None or price.interval not in INTERVALS_PER_MONTH:
        return None
    count = price.interval_count if price.interval_count and price.interval_count > 0 else 1
    return Fraction(price.unit_amount) * INTERVALS_PER_MONTH[price.interval] / count


@dataclass(frozen=True, slots=True)
class Mrr:
    """The MRR of one currency and how many subscriptions it counts."""

    currency: str
    amount: int
    subscriptions: int


@dataclass(frozen=True, slots=True)
class MrrSummary:
    """MRR per currency (sorted) and the billed subscriptions without a known price."""

    currencies: list[Mrr]
    unpriced: int


def mrr(tallies: Iterable[SubscriptionTally], prices: Mapping[str, PlanPrice]) -> MrrSummary:
    """Monthly recurring revenue per currency (see the module rules)."""
    totals: dict[str, Fraction] = defaultdict(Fraction)
    counted: dict[str, int] = defaultdict(int)
    unpriced = 0
    for tally in tallies:
        if tally.status not in MRR_STATUSES:
            continue
        price = prices.get(tally.plan_key or "")
        monthly = monthly_amount(price) if price else None
        if price is None or monthly is None or not price.currency:
            unpriced += tally.count
            continue
        currency = price.currency.lower()
        totals[currency] += monthly * tally.count
        counted[currency] += tally.count
    return MrrSummary(
        [Mrr(c, round_half_up(totals[c]), counted[c]) for c in sorted(totals)], unpriced
    )
