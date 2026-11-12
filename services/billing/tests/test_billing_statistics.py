# SPDX-FileCopyrightText: 2026 Lukas Körber
# SPDX-License-Identifier: AGPL-3.0-only

"""The pure rules of the payments dashboard (ADR 0083): month buckets in UTC, revenue per
currency with refunds taken off, MRR normalisation, counts, the ledger's keyed hashes and how a
later event updates a fact. Exact numbers throughout."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta, timezone
from fractions import Fraction
from uuid import uuid4
from zoneinfo import ZoneInfo

import pytest
from mowdeck_contracts.v1 import SubscriptionStatus

from billing.domain.currency import minor_unit_exponent
from billing.domain.ledger import (
    REF_HORIZON,
    FactContext,
    FactKind,
    PaymentFact,
    apply_update,
    dispute_fact,
    invoice_paid_fact,
    ledger_ref,
    manual_refund_fact,
    payment_failed_fact,
    refs_expire_before,
    refund_fact,
    subscription_facts,
)
from billing.domain.models import (
    ManualRefund,
    PlanPrice,
    ProviderDispute,
    ProviderInvoice,
    ProviderRefund,
    ProviderSubscription,
    RefundStatus,
)
from billing.domain.statistics import (
    OpenInvoice,
    SubscriptionTally,
    closed_disputes,
    disputes_by_month,
    failures_by_month,
    invoice_backlog,
    latest,
    month_of,
    monthly_amount,
    mrr,
    open_disputes,
    plan_counts,
    recent_months,
    refunds_by_month,
    revenue,
    round_half_up,
    subscription_flow,
    window_start,
)

NOW = datetime(2026, 11, 12, 9, 30, tzinfo=UTC)
BERLIN = ZoneInfo("Europe/Berlin")
KEY = b"ledger-test-key"


def fact(kind: FactKind, at: datetime, **values: object) -> PaymentFact:
    base = PaymentFact(
        id=uuid4(),
        kind=kind,
        ref=uuid4().hex,
        provider="stripe",
        occurred_at=at,
        event_at=at,
        recorded_at=at,
        updated_at=at,
    )
    return replace(base, **values)  # type: ignore[arg-type]


def ts(month: int, day: int = 15, hour: int = 12, year: int = 2026) -> datetime:
    return datetime(year, month, day, hour, tzinfo=UTC)


# --- months --------------------------------------------------------------------------------


def test_months_are_calendar_months_ending_with_now() -> None:
    assert recent_months(NOW, 3) == ["2026-09", "2026-10", "2026-11"]
    assert recent_months(datetime(2026, 2, 1, tzinfo=UTC), 3) == ["2025-12", "2026-01", "2026-02"]
    assert recent_months(NOW, 1) == ["2026-11"]
    assert window_start(NOW, 12) == datetime(2025, 12, 1, tzinfo=UTC)
    # Without a zone the buckets are UTC's: 00:30 on 1 December at +01:00 is still November.
    plus_one = timezone(timedelta(hours=1))
    assert month_of(datetime(2026, 12, 1, 0, 30, tzinfo=plus_one)) == "2026-11"
    assert month_of(datetime(2026, 12, 1, 0, 0, tzinfo=UTC)) == "2026-12"


def test_months_in_berlin_follow_local_midnight_across_dst() -> None:
    # 00:30 on 1 December in Berlin (23:30 UTC the day before) lands in December.
    assert month_of(datetime(2026, 11, 30, 23, 30, tzinfo=UTC), BERLIN) == "2026-12"
    assert month_of(datetime(2026, 11, 30, 22, 59, tzinfo=UTC), BERLIN) == "2026-11"
    # Summer time (+02:00) starts on 29 March 2026: 00:30 on 1 April is 22:30 UTC on 31 March.
    assert month_of(datetime(2026, 3, 31, 22, 30, tzinfo=UTC), BERLIN) == "2026-04"
    assert month_of(datetime(2026, 3, 31, 21, 59, tzinfo=UTC), BERLIN) == "2026-03"
    # Winter time (+01:00) is back on 25 October 2026: 1 November starts at 23:00 UTC.
    assert month_of(datetime(2026, 10, 31, 23, 0, tzinfo=UTC), BERLIN) == "2026-11"
    assert month_of(datetime(2026, 10, 31, 22, 59, tzinfo=UTC), BERLIN) == "2026-10"
    # The hour that repeats on 25 October stays in October.
    assert month_of(datetime(2026, 10, 25, 0, 30, tzinfo=UTC), BERLIN) == "2026-10"
    assert month_of(datetime(2026, 10, 25, 1, 30, tzinfo=UTC), BERLIN) == "2026-10"
    # The window starts at local midnight of its first month, with that month's offset.
    late = datetime(2026, 11, 30, 23, 30, tzinfo=UTC)
    assert recent_months(late, 2, BERLIN) == ["2026-11", "2026-12"]
    assert window_start(late, 2, BERLIN) == datetime(2026, 10, 31, 23, 0, tzinfo=UTC)
    assert window_start(datetime(2026, 4, 2, tzinfo=UTC), 1, BERLIN) == datetime(
        2026, 3, 31, 22, 0, tzinfo=UTC
    )
    assert window_start(datetime(2026, 8, 2, tzinfo=UTC), 6, BERLIN) == datetime(
        2026, 2, 28, 23, 0, tzinfo=UTC
    )


def test_aggregates_use_the_given_zone() -> None:
    at = datetime(2026, 10, 31, 23, 30, tzinfo=UTC)  # 00:30 on 1 November in Berlin
    facts = [fact(FactKind.INVOICE_PAID, at, currency="eur", amount=990)]
    months = ["2026-10", "2026-11"]
    (in_berlin,) = revenue(facts, months, BERLIN)
    assert [m.gross for m in in_berlin.months] == [0, 990]
    (in_utc,) = revenue(facts, months)
    assert [m.gross for m in in_utc.months] == [990, 0]
    flow = subscription_flow([fact(FactKind.SUBSCRIPTION_STARTED, at)], months, BERLIN)
    assert [(f.month, f.started) for f in flow] == [("2026-10", 0), ("2026-11", 1)]


# --- revenue -------------------------------------------------------------------------------


def test_revenue_per_currency_and_month_with_refunds_taken_off() -> None:
    months = recent_months(NOW, 3)
    facts = [
        fact(FactKind.INVOICE_PAID, ts(9), currency="eur", amount=990, tax=158),
        fact(FactKind.INVOICE_PAID, ts(10), currency="eur", amount=990, tax=158),
        fact(FactKind.INVOICE_PAID, ts(10, 20), currency="eur", amount=9900, tax=1581),
        fact(FactKind.INVOICE_PAID, ts(11), currency="chf", amount=1200),
        # Refunds count in the month they were created, only when they take money back.
        fact(FactKind.REFUND, ts(10, 25), currency="eur", amount=450, status="succeeded"),
        fact(FactKind.REFUND, ts(11), currency="eur", amount=300, status="pending"),
        fact(FactKind.REFUND, ts(11), currency="eur", amount=990, status="failed"),
        fact(FactKind.REFUND, ts(11), currency="eur", amount=100, status="canceled"),
        # Outside the window, and another kind: ignored.
        fact(FactKind.INVOICE_PAID, ts(8), currency="eur", amount=5000),
        fact(FactKind.PAYMENT_FAILED, ts(11), currency="eur", amount=990),
    ]
    chf, eur = revenue(facts, months)
    assert eur.currency == "eur"
    assert [(m.month, m.gross, m.tax, m.refunded, m.net) for m in eur.months] == [
        ("2026-09", 990, 158, 0, 990),
        ("2026-10", 10890, 1739, 450, 10440),
        ("2026-11", 0, 0, 300, -300),
    ]
    assert [(m.invoices, m.refunds) for m in eur.months] == [(1, 0), (2, 1), (0, 1)]
    assert (eur.gross, eur.refunded, eur.net) == (11880, 750, 11130)
    # Never added across currencies.
    assert chf.currency == "chf"
    assert [m.gross for m in chf.months] == [0, 0, 1200]
    assert chf.net == 1200
    assert revenue([], months) == []


def test_refunds_by_month_reason_and_currency() -> None:
    months = recent_months(NOW, 2)
    facts = [
        fact(FactKind.REFUND, ts(10), currency="eur", amount=450, status="succeeded",
             reason="erasure"),
        fact(FactKind.REFUND, ts(10, 2), currency="eur", amount=550, status="succeeded",
             reason="erasure"),
        fact(FactKind.REFUND, ts(10), currency="eur", amount=990, status="succeeded",
             reason="other"),
        fact(FactKind.REFUND, ts(11), currency="eur", amount=200, status="failed",
             reason="erasure"),
        fact(FactKind.REFUND, ts(11), currency="usd", amount=1000, status="requires_action",
             reason="other"),
    ]  # fmt: skip
    rows = [
        (r.month, r.currency, r.reason, r.count, r.amount) for r in refunds_by_month(facts, months)
    ]
    assert rows == [
        ("2026-10", "eur", "erasure", 2, 1000),
        ("2026-10", "eur", "other", 1, 990),
        ("2026-11", "usd", "other", 1, 1000),
    ]


# --- payment problems ----------------------------------------------------------------------


def test_failures_backlog_and_disputes() -> None:
    months = recent_months(NOW, 2)
    failures = [
        fact(FactKind.PAYMENT_FAILED, ts(10, 1), currency="eur", amount=990, attempt=1),
        fact(FactKind.PAYMENT_FAILED, ts(10, 4), currency="eur", amount=990, attempt=2),
        fact(FactKind.PAYMENT_FAILED, ts(11, 2), currency="eur", amount=9900, attempt=1),
    ]
    assert [
        (f.month, f.currency, f.count, f.amount) for f in failures_by_month(failures, months)
    ] == [
        ("2026-10", "eur", 2, 1980),
        ("2026-11", "eur", 1, 9900),
    ]
    assert [f.attempt for f in latest(failures, FactKind.PAYMENT_FAILED, 2)] == [1, 2]

    backlog = invoice_backlog(
        [
            OpenInvoice("eur", 990, NOW - timedelta(days=3)),
            OpenInvoice("EUR", 500, NOW + timedelta(days=1)),
            OpenInvoice("usd", 1000, NOW - timedelta(seconds=1)),
        ],
        NOW,
    )
    assert [
        (b.currency, b.open_count, b.open_amount, b.overdue_count, b.overdue_amount)
        for b in backlog
    ] == [("eur", 2, 1490, 1, 990), ("usd", 1, 1000, 1, 1000)]

    opened_late = fact(
        FactKind.DISPUTE,
        ts(11, 1),
        currency="eur",
        amount=990,
        status="needs_response",
        due_by=ts(11, 20),
    )
    opened_early = fact(
        FactKind.DISPUTE,
        ts(10, 1),
        currency="eur",
        amount=500,
        status="under_review",
        due_by=ts(11, 18),
    )
    no_due = fact(
        FactKind.DISPUTE, ts(10, 3), currency="eur", amount=100, status="warning_needs_response"
    )
    lost = fact(
        FactKind.DISPUTE, ts(10, 5), currency="eur", amount=700, status="lost", closed_at=ts(11, 3)
    )
    won = fact(
        FactKind.DISPUTE, ts(9, 5), currency="eur", amount=800, status="won", closed_at=ts(11, 8)
    )
    disputes = [opened_late, opened_early, no_due, lost, won]  # fmt: skip
    assert [d.id for d in open_disputes(disputes)] == [opened_early.id, opened_late.id, no_due.id]
    assert [d.id for d in closed_disputes(disputes, 5)] == [won.id, lost.id]
    assert [
        (d.month, d.currency, d.opened, d.amount, d.lost)
        for d in disputes_by_month(disputes, months)
    ] == [
        ("2026-10", "eur", 3, 1300, 1),
        ("2026-11", "eur", 1, 990, 0),
    ]


# --- subscriptions -------------------------------------------------------------------------


def test_subscription_flow_and_counts_per_plan() -> None:
    months = recent_months(NOW, 3)
    flow = subscription_flow(
        [
            fact(FactKind.SUBSCRIPTION_STARTED, ts(9)),
            fact(FactKind.SUBSCRIPTION_STARTED, ts(11)),
            fact(FactKind.SUBSCRIPTION_STARTED, ts(11, 2)),
            fact(FactKind.SUBSCRIPTION_ENDED, ts(11, 3)),
        ],
        months,
    )
    assert [(f.month, f.started, f.ended) for f in flow] == [
        ("2026-09", 1, 0),
        ("2026-10", 0, 0),
        ("2026-11", 2, 1),
    ]
    counts = plan_counts(
        [
            SubscriptionTally("home", SubscriptionStatus.ACTIVE, 7),
            SubscriptionTally("home", SubscriptionStatus.TRIALING, 2),
            SubscriptionTally("home", SubscriptionStatus.CANCELED, 30),
            SubscriptionTally("fleet", SubscriptionStatus.PAST_DUE, 1),
            SubscriptionTally(None, SubscriptionStatus.ACTIVE, 1),
            SubscriptionTally("old", SubscriptionStatus.CANCELED, 4),
        ]
    )
    assert [(c.plan_key, c.trialing, c.active, c.past_due, c.total) for c in counts] == [
        ("fleet", 0, 0, 1, 1),
        ("home", 2, 7, 0, 9),
        (None, 0, 1, 0, 1),
    ]


def price(plan: str, amount: int | None, interval: str | None, count: int = 1,
          currency: str = "eur") -> PlanPrice:  # fmt: skip
    return PlanPrice(plan, f"price_{plan}", currency, amount, interval, count)


def test_monthly_amount_normalises_every_interval() -> None:
    assert monthly_amount(price("m", 990, "month")) == Fraction(990)
    assert monthly_amount(price("y", 9900, "year")) == Fraction(825)
    assert monthly_amount(price("y", 10000, "year")) == Fraction(2500, 3)
    assert monthly_amount(price("q", 2970, "month", 3)) == Fraction(990)
    assert monthly_amount(price("w", 300, "week")) == Fraction(1300)
    assert monthly_amount(price("d", 12, "day")) == Fraction(365)
    assert monthly_amount(price("x", None, "month")) is None
    assert monthly_amount(price("x", 990, None)) is None
    assert monthly_amount(price("x", 990, "fortnight")) is None
    assert monthly_amount(price("z", 990, "month", 0)) == Fraction(990)


def test_mrr_counts_billed_subscriptions_per_currency_and_rounds_once() -> None:
    prices = {
        "home": price("home", 990, "month"),
        "yearly": price("yearly", 10000, "year"),  # 833.33... a month
        "fleet": price("fleet", 4900, "month", currency="usd"),
        "broken": price("broken", None, None),
    }
    summary = mrr(
        [
            SubscriptionTally("home", SubscriptionStatus.ACTIVE, 3),
            SubscriptionTally("home", SubscriptionStatus.PAST_DUE, 1),
            SubscriptionTally("home", SubscriptionStatus.TRIALING, 5),  # pays nothing yet
            SubscriptionTally("home", SubscriptionStatus.CANCELED, 9),
            SubscriptionTally("yearly", SubscriptionStatus.ACTIVE, 2),
            SubscriptionTally("fleet", SubscriptionStatus.ACTIVE, 1),
            SubscriptionTally("broken", SubscriptionStatus.ACTIVE, 1),
            SubscriptionTally(None, SubscriptionStatus.ACTIVE, 2),
            SubscriptionTally("gone", SubscriptionStatus.PAST_DUE, 1),
        ],
        prices,
    )
    # 4 x 990 + 2 x 10000/12 = 3960 + 1666.67: rounded once, to 5627.
    assert [(m.currency, m.amount, m.subscriptions) for m in summary.currencies] == [
        ("eur", 5627, 6),
        ("usd", 4900, 1),
    ]
    assert summary.unpriced == 4
    assert round_half_up(Fraction(5, 2)) == 3
    assert round_half_up(Fraction(49, 20)) == 2


# --- currencies ----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("currency", "exponent"),
    [("eur", 2), ("EUR", 2), ("jpy", 0), ("krw", 0), ("kwd", 3), ("isk", 2), ("ugx", 2)],
)
def test_minor_unit_exponents_follow_stripe(currency: str, exponent: int) -> None:
    assert minor_unit_exponent(currency) == exponent


# --- the ledger's facts --------------------------------------------------------------------


def ctx(at: datetime = NOW) -> FactContext:
    return FactContext(KEY, "stripe", at, NOW)


def test_ledger_ref_is_a_keyed_hash_without_the_object_id() -> None:
    ref = ledger_ref(KEY, "stripe", FactKind.REFUND, "re_123")
    assert len(ref) == 64 and "re_123" not in ref
    assert ref == ledger_ref(KEY, "stripe", FactKind.REFUND, "re_123")
    assert ref != ledger_ref(b"other key", "stripe", FactKind.REFUND, "re_123")
    assert ref != ledger_ref(KEY, "stripe", FactKind.DISPUTE, "re_123")
    assert refs_expire_before(NOW) == NOW - REF_HORIZON


def invoice(**values: object) -> ProviderInvoice:
    base = ProviderInvoice(
        id="in_1",
        customer_id="cus_1",
        status="paid",
        currency="EUR",
        amount_due=1178,
        amount_paid=1178,
        created=ts(10),
        tax=188,
        paid_at=ts(10, 16),
        attempt_count=1,
        period_start=ts(10, 16),
        period_end=ts(11, 16),
    )
    return replace(base, **values)  # type: ignore[arg-type]


def test_facts_of_invoices() -> None:
    paid = invoice_paid_fact(ctx(), invoice(), "home")
    assert paid is not None
    assert (paid.kind, paid.currency, paid.amount, paid.tax, paid.plan_key) == (
        FactKind.INVOICE_PAID,
        "eur",
        1178,
        188,
        "home",
    )
    assert paid.occurred_at == ts(10, 16)  # when it was paid, not when it was issued
    assert paid.period_start == ts(10, 16)
    assert invoice_paid_fact(ctx(), invoice(amount_paid=0), "home") is None
    assert invoice_paid_fact(ctx(), invoice(status="open"), "home") is None

    first = payment_failed_fact(ctx(), invoice(status="open", amount_paid=0), "home")
    second = payment_failed_fact(
        ctx(), invoice(status="open", amount_paid=0, attempt_count=2), "home"
    )
    assert (first.amount, first.attempt, second.attempt) == (1178, 1, 2)
    assert first.ref != second.ref  # every attempt is its own fact
    assert first.occurred_at == NOW


def test_facts_of_refunds_disputes_and_subscriptions() -> None:
    refund = ProviderRefund(
        "re_1", "pi_1", 450, "eur", RefundStatus.SUCCEEDED,
        metadata={"reason": "account_erasure"}, created=ts(11, 2),
    )  # fmt: skip
    erased = refund_fact(ctx(), refund)
    assert (erased.reason, erased.status, erased.occurred_at) == ("erasure", "succeeded", ts(11, 2))
    assert refund_fact(ctx(), replace(refund, metadata={})).reason == "other"

    manual = ManualRefund(uuid4(), "pi_1:in_1", "pi_1", "in_1", 600, "eur", "refused", ts(10))
    assert manual_refund_fact(ctx(), manual) is None
    settled = manual_refund_fact(ctx(), replace(manual, settled_at=ts(11, 5)))
    assert settled is not None
    assert (settled.reason, settled.status, settled.amount, settled.occurred_at) == (
        "erasure",
        "succeeded",
        600,
        ts(11, 5),
    )

    dispute = ProviderDispute("dp_1", 990, "eur", "needs_response", "fraudulent", ts(10, 3),
                              due_by=ts(10, 20))  # fmt: skip
    opened = dispute_fact(ctx(ts(10, 3)), dispute)
    assert (opened.closed_at, opened.due_by, opened.occurred_at) == (None, ts(10, 20), ts(10, 3))
    closed = dispute_fact(ctx(ts(11, 1)), replace(dispute, status="lost"))
    assert closed.closed_at == ts(11, 1) and closed.ref == opened.ref

    sub = ProviderSubscription("sub_1", "cus_1", SubscriptionStatus.TRIALING, started_at=ts(9))
    assert [(f.kind, f.occurred_at) for f in subscription_facts(ctx(), sub, "home")] == [
        (FactKind.SUBSCRIPTION_STARTED, ts(9))
    ]
    incomplete = replace(sub, status=SubscriptionStatus.INCOMPLETE)
    assert subscription_facts(ctx(), incomplete, "home") == []
    ended = replace(sub, status=SubscriptionStatus.CANCELED, ended_at=ts(11, 4))
    assert [(f.kind, f.occurred_at) for f in subscription_facts(ctx(), ended, "home")] == [
        (FactKind.SUBSCRIPTION_ENDED, ts(11, 4))
    ]


def test_a_later_event_updates_an_older_one_changes_nothing() -> None:
    stored = fact(FactKind.REFUND, ts(10), status="pending", amount=450, event_at=ts(10))
    newer = replace(stored, id=uuid4(), status="succeeded", event_at=ts(10, 16), updated_at=NOW)
    updated = apply_update(stored, newer)
    assert updated is not None
    assert (updated.id, updated.status, updated.event_at, updated.updated_at) == (
        stored.id,
        "succeeded",
        ts(10, 16),
        NOW,
    )
    # The pending event arriving late, and the same event again: nothing changes.
    assert apply_update(updated, stored) is None
    assert apply_update(updated, replace(newer, updated_at=NOW + timedelta(hours=1))) is None
    # Fields written once stay (the occurrence, the currency, the plan).
    moved = replace(newer, occurred_at=ts(11), currency="usd", status="failed", event_at=ts(11))
    changed = apply_update(updated, moved)
    assert changed is not None
    assert (changed.occurred_at, changed.currency, changed.status) == (ts(10), None, "failed")
