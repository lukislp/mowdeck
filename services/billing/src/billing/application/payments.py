# SPDX-FileCopyrightText: 2026 Lukas Körber
# SPDX-License-Identifier: AGPL-3.0-only

"""The operator's payments dashboard (ADR 0083): one summary of the last months.

Everything is read in one transaction (a consistent picture) and aggregated by the pure rules
of :mod:`billing.domain.statistics`: the ledger facts of the window, the live subscription
counts and the open invoices. The MRR prices come from the same cache as ``GET /status``; the
provider is never asked for statistics. No answer names an account, a customer or a provider
object.
"""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, tzinfo

from mowdeck_contracts.v1 import SubscriptionStatus

from billing.application.ports import Clock, Store
from billing.domain.ledger import FactKind, PaymentFact
from billing.domain.models import PlanPrice
from billing.domain.statistics import (
    DEFAULT_ZONE,
    CurrencyRevenue,
    DisputeMonth,
    FailureMonth,
    FlowMonth,
    InvoiceBacklog,
    MrrSummary,
    PlanCount,
    RefundMonth,
    closed_disputes,
    disputes_by_month,
    failures_by_month,
    invoice_backlog,
    latest,
    mrr,
    open_disputes,
    plan_counts,
    recent_months,
    refunds_by_month,
    revenue,
    subscription_flow,
    window_start,
)

DEFAULT_MONTHS = 12
MAX_MONTHS = 36
RECENT_LIMIT = 10
"""How many recent failures and closed disputes the summary lists."""


@dataclass(frozen=True, slots=True)
class PaymentsSummary:
    """Every section of the dashboard for a window of ``months`` (oldest first)."""

    generated_at: datetime
    time_zone: str
    months: list[str]
    since: datetime | None
    revenue: list[CurrencyRevenue]
    plans: list[PlanCount]
    mrr: MrrSummary
    flow: list[FlowMonth]
    refunds: list[RefundMonth]
    failures: list[FailureMonth]
    recent_failures: list[PaymentFact]
    backlog: list[InvoiceBacklog]
    past_due: int
    disputes: list[DisputeMonth]
    open_disputes: list[PaymentFact]
    closed_disputes: list[PaymentFact]

    @property
    def currencies(self) -> set[str]:
        """Every currency the summary shows an amount in."""
        found = {r.currency for r in self.revenue}
        found |= {m.currency for m in self.mrr.currencies}
        found |= {r.currency for r in self.refunds}
        found |= {f.currency for f in self.failures}
        found |= {b.currency for b in self.backlog}
        found |= {d.currency for d in self.disputes}
        lists = (self.recent_failures, self.open_disputes, self.closed_disputes)
        found |= {f.currency for facts in lists for f in facts if f.currency}
        return {c for c in found if c}


def zone_name(zone: tzinfo) -> str:
    """The IANA name of a zone (``Europe/Berlin``), ``UTC`` for the UTC singleton."""
    return str(getattr(zone, "key", None) or zone.tzname(None) or "UTC")


class PaymentsDashboard:
    """Reads and aggregates; writes nothing."""

    def __init__(
        self,
        store: Store,
        clock: Clock,
        prices: Callable[[], Awaitable[dict[str, PlanPrice]]],
        zone: tzinfo = DEFAULT_ZONE,
    ) -> None:
        self._store = store
        self._clock = clock
        self._prices = prices
        self._zone = zone

    async def summary(self, months: int = DEFAULT_MONTHS) -> PaymentsSummary:
        """The dashboard of the last ``months`` months (this one included)."""
        now = self._clock()
        zone = self._zone
        window = recent_months(now, months, zone)
        async with self._store.transaction() as tx:
            facts = await tx.payment_facts(window_start(now, months, zone))
            since = await tx.first_fact_at()
            tallies = await tx.subscription_tally()
            invoices = await tx.open_invoices()
        prices = await self._prices()
        return PaymentsSummary(
            generated_at=now,
            time_zone=zone_name(zone),
            months=window,
            since=since,
            revenue=revenue(facts, window, zone),
            plans=plan_counts(tallies),
            mrr=mrr(tallies, prices),
            flow=subscription_flow(facts, window, zone),
            refunds=refunds_by_month(facts, window, zone),
            failures=failures_by_month(facts, window, zone),
            recent_failures=latest(facts, FactKind.PAYMENT_FAILED, RECENT_LIMIT),
            backlog=invoice_backlog(invoices, now),
            past_due=sum(t.count for t in tallies if t.status is SubscriptionStatus.PAST_DUE),
            disputes=disputes_by_month(facts, window, zone),
            open_disputes=open_disputes(facts),
            closed_disputes=closed_disputes(facts, RECENT_LIMIT),
        )
