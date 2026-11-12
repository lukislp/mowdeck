# SPDX-FileCopyrightText: 2026 Lukas Körber
# SPDX-License-Identifier: AGPL-3.0-only

"""The refund of an erased account (ADR 0082, section 5): the pure rule in
``billing.domain.refunds``, with exact numbers.

A monthly period of 30 days (1 to 30 November 2026) paid with 9.00 EUR unless a test says
otherwise. At the middle (16 November, 00:00) half of it is unused.
"""

from datetime import UTC, datetime, timedelta
from fractions import Fraction
from uuid import UUID

import pytest

from billing.domain.models import (
    InvoiceLine,
    LineKind,
    PaidInvoice,
    ProviderRefund,
    RefundStatus,
)
from billing.domain.refunds import (
    MANUAL_REFUND_RETENTION,
    MAX_FAILED_REFUNDS,
    RefundAction,
    covers,
    earliest_invoice,
    erasure_metadata,
    erasure_refund_lost,
    idempotency_key,
    lost_refund_record,
    manual_refund,
    manual_refund_key,
    plan_refund,
    refund_amount,
    round_down,
    settled_before,
    skipped,
    unused_fraction,
)

START = datetime(2026, 11, 1, tzinfo=UTC)
END = datetime(2026, 12, 1, tzinfo=UTC)
MIDDLE = datetime(2026, 11, 16, tzinfo=UTC)
SECOND = timedelta(seconds=1)
ACCOUNT = UUID("6f1c2a52-0d4e-4b8c-9a1e-3c5d7e9f1a2b")


def line(
    amount: int,
    kind: LineKind = LineKind.RECURRING,
    start: datetime = START,
    end: datetime = END,
) -> InvoiceLine:
    return InvoiceLine(amount=amount, kind=kind, period_start=start, period_end=end)


def invoice(
    paid: int = 900,
    *lines: InvoiceLine,
    status: str = "paid",
    currency: str = "eur",
    payment: str | None = "pi_1",
    payment_amount: int | None = None,
) -> PaidInvoice:
    return PaidInvoice(
        invoice_id="in_1",
        subscription_id="sub_1",
        status=status,
        currency=currency,
        amount_paid=paid,
        lines=lines or (line(paid),),
        payment_ref=payment,
        payment_amount=paid if payment_amount is None else payment_amount,
    )


def refund(
    amount: int,
    status: RefundStatus = RefundStatus.SUCCEEDED,
    metadata: dict[str, str] | None = None,
) -> ProviderRefund:
    return ProviderRefund(
        id=f"re_{amount}_{status.value}",
        payment_ref="pi_1",
        amount=amount,
        currency="eur",
        status=status,
        metadata=metadata or {},
    )


def ours(status: RefundStatus, amount: int = 450) -> ProviderRefund:
    return refund(amount, status, erasure_metadata(ACCOUNT, "sub_1", "in_1"))


# --- the unused part -----------------------------------------------------------------------------


def test_the_unused_part_is_time_based() -> None:
    assert unused_fraction(START, END, START) == 1
    assert unused_fraction(START, END, MIDDLE) == Fraction(1, 2)
    assert unused_fraction(START, END, END) == 0
    assert unused_fraction(START, END, START - SECOND) == 1
    assert unused_fraction(START, END, END + SECOND) == 0
    assert unused_fraction(START, START, START) == 0
    # 1 second of 30 days: exact, not a float
    assert unused_fraction(START, END, END - SECOND) == Fraction(1, 30 * 86_400)


# --- the amount ----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("now", "expected"),
    [
        (START, 900),  # erased at the start: everything back
        (START + timedelta(days=10), 600),  # 20 of 30 days unused
        (MIDDLE, 450),
        (datetime(2026, 11, 11, 12, tzinfo=UTC), 585),  # 19.5 of 30 days
        (datetime(2026, 11, 21, tzinfo=UTC) + SECOND, 299),  # 299.9997 rounded down
        (END - SECOND, 0),  # 0.0003 cents
        (END, 0),  # the period is over
        (END + timedelta(days=3), 0),
    ],
)
def test_a_monthly_period_is_refunded_for_the_unused_days(now: datetime, expected: int) -> None:
    assert refund_amount(invoice(), [], now) == expected


def test_a_yearly_period() -> None:
    year = invoice(
        9900,
        line(9900, start=datetime(2026, 1, 1, tzinfo=UTC), end=END.replace(year=2027, month=1)),
    )
    # 12 November to 1 January: 50 of 365 days; 9900 x 50 / 365 = 1356.16
    assert refund_amount(year, [], datetime(2026, 11, 12, tzinfo=UTC)) == 1356


def test_what_was_paid_counts_not_the_price() -> None:
    # A 20 % coupon: the line is 10.00, 8.00 were paid.
    assert refund_amount(invoice(800, line(1000)), [], MIDDLE) == 400
    # 4.00 came from the credit balance, 5.00 from the card.
    assert refund_amount(invoice(500, line(900)), [], MIDDLE) == 250
    # With 19 % VAT on top of 9.00: 10.71 paid, 5.355 rounded down.
    assert refund_amount(invoice(1071, line(900)), [], MIDDLE) == 535


def test_metered_usage_and_one_off_items_are_not_refunded() -> None:
    """Usage bills consumption that has already happened (the period before, in arrears): none
    of it is unused. A one-off item gets its share of what was paid, which is not refunded."""
    usage = line(300, start=datetime(2026, 10, 1, tzinfo=UTC), end=START)
    # 12.00 paid: 9.00 for this month's price, 3.00 for last month's metered usage.
    assert refund_amount(invoice(1200, line(900), usage), [], MIDDLE) == 450
    one_off = line(200, LineKind.OTHER)
    assert refund_amount(invoice(1100, line(900), one_off), [], MIDDLE) == 450
    # A coupon on the whole invoice is shared in proportion: 10.00 paid of 15.00 billed.
    assert refund_amount(invoice(1000, line(900), line(600, LineKind.OTHER)), [], MIDDLE) == 300
    # An invoice of usage alone (the period of the line is over): nothing, and not looked at.
    assert refund_amount(invoice(300, usage), [], MIDDLE) == 0
    assert not covers(invoice(300, usage), MIDDLE)


def test_a_proration_on_the_same_invoice_is_refunded_over_its_own_period() -> None:
    # 9.00 for the month and 3.00 for an upgrade from the 16th, on one invoice of 12.00.
    upgrade = line(300, LineKind.PRORATION, start=MIDDLE)
    erased = datetime(2026, 11, 21, tzinfo=UTC)
    # 9.00 x 10/30 + 3.00 x 10/15 = 3.00 + 2.00
    assert refund_amount(invoice(1200, line(900), upgrade), [], erased) == 500
    assert covers(invoice(300, upgrade), erased)
    assert not covers(invoice(300, upgrade), START)  # the upgrade had not begun


def test_an_upgrade_on_its_own_invoice_refunds_exactly_the_unused_new_plan() -> None:
    """Plan A (9.00 a month) paid on 1 November; on the 16th an upgrade to plan B (12.00 a
    month) is billed on its own invoice: a credit of 4.50 for A's unused half and a charge of
    6.00 for B's half, 1.50 paid. Erased on the 21st: 10 days of B are unused, 4.00.

    Each invoice is refunded for its own unused time: A's invoice 9.00 x 10/30 = 3.00, and of
    the upgrade invoice the 1.50 paid, shared among its positive lines (the 6.00 of B alone),
    x 10/15 = 1.00. Together 4.00. The credit is not a line to share among: sharing over all
    lines (6.00 - 4.50 = 1.50) would give B's line the whole 6.00 and refund too much."""
    erased = datetime(2026, 11, 21, tzinfo=UTC)
    month = invoice(900)
    upgrade = PaidInvoice(
        invoice_id="in_2",
        subscription_id="sub_1",
        status="paid",
        currency="eur",
        amount_paid=150,
        lines=(
            line(-450, LineKind.PRORATION, start=MIDDLE),
            line(600, LineKind.PRORATION, start=MIDDLE),
        ),
        payment_ref="pi_2",
        payment_amount=150,
    )
    assert covers(month, erased)
    assert covers(upgrade, erased)
    assert refund_amount(month, [], erased) == 300
    assert refund_amount(upgrade, [], erased) == 100
    # A downgrade whose credit is larger than the charge: nothing was paid on that invoice.
    downgrade = PaidInvoice(
        invoice_id="in_3",
        subscription_id="sub_1",
        status="paid",
        currency="eur",
        amount_paid=0,
        lines=(
            line(-450, LineKind.PRORATION, start=MIDDLE),
            line(300, LineKind.PRORATION, start=MIDDLE),
        ),
        payment_ref=None,
    )
    assert refund_amount(downgrade, [], erased) == 0


def test_a_credit_line_lowers_what_was_paid_and_is_not_shared() -> None:
    # 9.00 for the month, 3.00 off as a credit line: 6.00 paid, all of it for the month.
    credit = line(-300, LineKind.OTHER)
    assert refund_amount(invoice(600, line(900), credit), [], MIDDLE) == 300
    # Only credits and nothing positive: nothing to share.
    assert refund_amount(invoice(600, credit), [], MIDDLE) == 0


def test_what_was_refunded_already_is_taken_off() -> None:
    # 3.00 refunded in the dashboard: 6.00 left, half of it unused.
    assert refund_amount(invoice(), [refund(300)], MIDDLE) == 300
    assert refund_amount(invoice(), [refund(300, RefundStatus.PENDING)], MIDDLE) == 300
    # Failed and canceled refunds took nothing back.
    failed = [refund(300, RefundStatus.FAILED), refund(200, RefundStatus.CANCELED)]
    assert refund_amount(invoice(), failed, MIDDLE) == 450
    # Refunded in full: nothing.
    assert refund_amount(invoice(), [refund(900)], MIDDLE) == 0
    assert refund_amount(invoice(), [refund(1000)], MIDDLE) == 0


def test_never_more_than_is_left_of_the_payment() -> None:
    # The card paid 3.00 of the 9.00 (the rest came from a balance paid out otherwise).
    assert refund_amount(invoice(900, payment_amount=300), [], START) == 300
    assert refund_amount(invoice(900, payment_amount=300), [refund(250)], START) == 50


@pytest.mark.parametrize(
    ("paid", "status", "payment"),
    [
        (0, "paid", "pi_1"),  # a trial, or a 100 % coupon: the invoice is 0
        (900, "open", "pi_1"),  # not paid
        (900, "uncollectible", "pi_1"),
        (900, "paid", None),  # paid by the credit balance alone, or outside the provider
    ],
)
def test_nothing_paid_nothing_refunded(paid: int, status: str, payment: str | None) -> None:
    assert refund_amount(invoice(paid, line(900), status=status, payment=payment), [], MIDDLE) == 0
    assert refund_amount(None, [], MIDDLE) == 0


def test_zero_decimal_and_special_currencies() -> None:
    # JPY has no decimals: 1000 is 1000 yen, and a third of it is 333 yen (not 333.33).
    yen = invoice(1000, currency="jpy")
    assert refund_amount(yen, [], datetime(2026, 11, 21, tzinfo=UTC) + SECOND) == 333
    # ISK is written with two decimals but refunded in whole kronur: a multiple of 100.
    kronur = invoice(990_000, currency="isk")
    assert refund_amount(kronur, [], datetime(2026, 11, 21, tzinfo=UTC)) == 330_000
    assert refund_amount(kronur, [], datetime(2026, 11, 21, tzinfo=UTC) + SECOND) == 329_900
    assert round_down(Fraction(12_399), "UGX") == 12_300
    assert round_down(Fraction(-5), "eur") == 0


# --- the decision ------------------------------------------------------------------------------


def test_a_paid_period_is_refunded_under_a_key_and_metadata_of_the_erasure() -> None:
    plan = plan_refund(ACCOUNT, invoice(), [], MIDDLE)
    assert plan.action is RefundAction.CREATE
    assert (plan.amount, plan.currency, plan.payment_ref) == (450, "eur", "pi_1")
    assert plan.idempotency_key == f"mowdeck-erasure-refund-{ACCOUNT}-sub_1-in_1"
    assert plan.metadata == {
        "reason": "account_erasure",
        "account_id": str(ACCOUNT),
        "subscription_id": "sub_1",
        "invoice_id": "in_1",
    }


@pytest.mark.parametrize(
    "status", [RefundStatus.PENDING, RefundStatus.SUCCEEDED, RefundStatus.REQUIRES_ACTION]
)
def test_a_refund_an_earlier_attempt_made_is_not_made_again(status: RefundStatus) -> None:
    """Later the unused part is smaller; that changes nothing, the refund is done."""
    made = ours(status, 600)
    plan = plan_refund(ACCOUNT, invoice(), [made], MIDDLE)
    assert plan.action is RefundAction.DONE
    assert plan.existing == made
    assert plan.amount == 600


def test_a_refund_of_another_erasure_or_the_dashboard_is_not_ours() -> None:
    other = refund(300, metadata=erasure_metadata(ACCOUNT, "sub_1", "in_0"))
    plan = plan_refund(ACCOUNT, invoice(), [other], MIDDLE)
    assert plan.action is RefundAction.CREATE
    assert plan.amount == 300  # but it took 3.00 back


def test_after_a_failed_refund_the_next_one_gets_a_new_key() -> None:
    once = plan_refund(ACCOUNT, invoice(), [ours(RefundStatus.FAILED)], MIDDLE)
    twice = plan_refund(
        ACCOUNT, invoice(), [ours(RefundStatus.FAILED), ours(RefundStatus.CANCELED)], MIDDLE
    )
    assert (once.action, once.amount) == (RefundAction.CREATE, 450)
    assert once.idempotency_key == idempotency_key(ACCOUNT, "sub_1", "in_1", 1)
    assert once.idempotency_key.endswith("-in_1-1")
    assert twice.idempotency_key.endswith("-in_1-2")


def test_after_too_many_failed_refunds_the_refund_is_skipped_with_its_amount() -> None:
    failed = [ours(RefundStatus.FAILED) for _ in range(MAX_FAILED_REFUNDS)]
    plan = plan_refund(ACCOUNT, invoice(), failed, MIDDLE)
    assert plan.action is RefundAction.SKIP
    assert (plan.amount, plan.currency, plan.payment_ref) == (450, "eur", "pi_1")
    assert plan.reason == "3 refunds failed"
    assert MAX_FAILED_REFUNDS == 3


def test_a_refusal_for_good_skips_the_refund_with_its_amount() -> None:
    plan = skipped(plan_refund(ACCOUNT, invoice(), [], MIDDLE), "the payment is too old")
    assert plan.action is RefundAction.SKIP
    assert (plan.amount, plan.reason) == (450, "the payment is too old")
    assert plan.metadata["account_id"] == str(ACCOUNT)


def test_nothing_to_refund() -> None:
    assert plan_refund(ACCOUNT, None, [], MIDDLE).action is RefundAction.NONE
    assert plan_refund(ACCOUNT, invoice(0, line(900)), [], MIDDLE).action is RefundAction.NONE
    assert plan_refund(ACCOUNT, invoice(), [], END).action is RefundAction.NONE
    # Even after failed refunds: nothing to skip and nothing to warn about.
    failed = [ours(RefundStatus.FAILED) for _ in range(MAX_FAILED_REFUNDS)]
    assert plan_refund(ACCOUNT, invoice(), failed, END).action is RefundAction.NONE


def test_a_refund_of_an_erasure_that_failed_later_is_lost() -> None:
    for status in (RefundStatus.FAILED, RefundStatus.CANCELED):
        lost = ours(status)
        assert erasure_refund_lost(lost, [lost])
    assert not erasure_refund_lost(ours(RefundStatus.PENDING), [])
    assert not erasure_refund_lost(ours(RefundStatus.SUCCEEDED), [])
    assert not erasure_refund_lost(refund(300, RefundStatus.FAILED), [])  # not an erasure's


def test_a_failed_refund_that_a_later_attempt_replaced_is_not_lost() -> None:
    failed = ours(RefundStatus.FAILED)
    replaced = ProviderRefund(
        id="re_2",
        payment_ref="pi_1",
        amount=400,
        currency="eur",
        status=RefundStatus.PENDING,
        metadata=failed.metadata,
    )
    assert not erasure_refund_lost(failed, [failed, replaced])
    other_invoice = ProviderRefund(
        id="re_3",
        payment_ref="pi_1",
        amount=400,
        currency="eur",
        status=RefundStatus.SUCCEEDED,
        metadata=erasure_metadata(ACCOUNT, "sub_1", "in_0"),
    )
    assert erasure_refund_lost(failed, [failed, other_invoice])


# --- the record of refunds the operator pays by hand ------------------------------------------


def test_a_skipped_refund_is_recorded_without_the_account() -> None:
    plan = skipped(plan_refund(ACCOUNT, invoice(), [], MIDDLE), "payment too old")
    record = manual_refund(plan, MIDDLE)
    assert (record.payment_ref, record.invoice_ref) == ("pi_1", "in_1")
    assert (record.amount, record.currency, record.reason) == (450, "eur", "payment too old")
    assert record.key == manual_refund_key("pi_1", "in_1") == "pi_1:in_1"
    assert (record.created_at, record.settled_at, record.is_open) == (MIDDLE, None, True)
    assert str(ACCOUNT) not in repr(record)


def test_a_lost_refund_is_recorded_under_the_same_key_as_a_skipped_one() -> None:
    failed = ProviderRefund(
        id="re_9",
        payment_ref="pi_1",
        amount=450,
        currency="eur",
        status=RefundStatus.FAILED,
        metadata=erasure_metadata(ACCOUNT, "sub_1", "in_1"),
        failure_reason="expired_or_canceled_card",
    )
    record = lost_refund_record(failed, MIDDLE)
    assert record.key == "pi_1:in_1"
    assert (record.amount, record.invoice_ref) == (450, "in_1")
    assert record.reason == "refund re_9 failed (expired_or_canceled_card)"
    assert str(ACCOUNT) not in repr(record)


def test_settled_records_are_kept_for_the_retention() -> None:
    assert timedelta(days=30) == MANUAL_REFUND_RETENTION
    assert settled_before(MIDDLE) == MIDDLE - timedelta(days=30)


def test_invoices_are_looked_for_one_longest_period_back() -> None:
    assert earliest_invoice(MIDDLE) == MIDDLE - timedelta(days=366)
