# SPDX-FileCopyrightText: 2026 Lukas Körber
# SPDX-License-Identifier: AGPL-3.0-only

"""The credit balance of an erased account is paid back (ADR 0082, section 5): the pure rule in
``billing.domain.credit``, with exact numbers.

Three payments of 9.00 EUR unless a test says otherwise: November (the newest), October and
September. Amounts are in cents.
"""

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from uuid import UUID

from billing.domain.credit import (
    clearing_idempotency_key,
    clearing_metadata,
    credit_idempotency_key,
    credit_leftover_key,
    credit_metadata,
    credit_owed,
    credit_records,
    credit_skipped,
    earliest_payment,
    is_clearing_metadata,
    plan_credit_refunds,
)
from billing.domain.models import CreditBalance, ProviderRefund, RecentPayment, RefundStatus
from billing.domain.refunds import (
    MAX_FAILED_REFUNDS,
    RefundAction,
    erasure_metadata,
    is_erasure_metadata,
    lost_refund_record,
    manual_refund_key,
)

ACCOUNT = UUID("6f1c2a52-0d4e-4b8c-9a1e-3c5d7e9f1a2b")
CUSTOMER = "cus_1"
NOW = datetime(2026, 11, 16, tzinfo=UTC)

Payments = list[tuple[RecentPayment, Sequence[ProviderRefund]]]


def payment(
    ref: str, days_ago: int, amount: int = 900, currency: str = "eur", refunded: int = 0
) -> RecentPayment:
    return RecentPayment(ref, amount, currency, NOW - timedelta(days=days_ago), refunded)


def refund(
    ref: str,
    amount: int,
    status: RefundStatus = RefundStatus.SUCCEEDED,
    metadata: dict[str, str] | None = None,
) -> ProviderRefund:
    return ProviderRefund(
        f"re_{ref}_{amount}_{status.value}", ref, amount, "eur", status, metadata or {}
    )


def three(*, refunds: dict[str, list[ProviderRefund]] | None = None) -> Payments:
    """September, October, November, oldest first (the rule sorts them)."""
    refunds = refunds or {}
    return [
        (payment(ref, days), refunds.get(ref, []))
        for ref, days in (("pi_sep", 76), ("pi_oct", 46), ("pi_nov", 16))
    ]


def credit(amount: int, currency: str = "eur", cleared: int = 0) -> CreditBalance:
    return CreditBalance(amount, currency, cleared)


def placed(plan_refunds: Sequence[object]) -> list[tuple[str, str | None, int]]:
    return [(p.action.value, p.payment_ref, p.amount) for p in plan_refunds]  # type: ignore[attr-defined]


def test_a_balance_is_spread_over_three_payments_newest_first() -> None:
    """25.00 EUR of credit: 9.00 from November, 9.00 from October, 7.00 from September."""
    plan = plan_credit_refunds(ACCOUNT, CUSTOMER, credit(2500), three())

    assert (plan.credit, plan.currency, plan.leftover, plan.clear) == (2500, "eur", 0, 2500)
    assert placed(plan.refunds) == [
        ("create", "pi_nov", 900),
        ("create", "pi_oct", 900),
        ("create", "pi_sep", 700),
    ]
    nov = plan.refunds[0]
    assert nov.metadata == credit_metadata(ACCOUNT, "pi_nov")
    assert nov.metadata == {
        "reason": "account_erasure_credit_balance",
        "account_id": str(ACCOUNT),
        "payment": "pi_nov",
    }
    assert nov.idempotency_key == f"mowdeck-erasure-credit-{ACCOUNT}-pi_nov"
    assert nov.invoice_id is None
    assert plan.clear_key == f"mowdeck-erasure-credit-clear-{ACCOUNT}-{CUSTOMER}-2500eur"
    assert plan.clear_metadata == clearing_metadata(ACCOUNT)


def test_a_small_balance_takes_the_newest_payment_only() -> None:
    plan = plan_credit_refunds(ACCOUNT, CUSTOMER, credit(450), three())

    assert placed(plan.refunds) == [("create", "pi_nov", 450)]
    assert (plan.leftover, plan.clear) == (0, 450)


def test_partly_refunded_payments_take_what_is_left_and_the_rest_is_recorded() -> None:
    """The worked example of the module: November's period refund of this erasure (6.00) leaves
    3.00, a dashboard refund of 1.00 leaves 8.00 of October, September is whole. 25.00 of credit:
    3.00 + 8.00 + 9.00 refunded, 5.00 recorded for the operator, the balance zeroed with 25.00."""
    period = erasure_metadata(ACCOUNT, "sub_1", "in_nov")
    payments = three(
        refunds={
            "pi_nov": [refund("pi_nov", 600, RefundStatus.PENDING, period)],
            "pi_oct": [
                refund("pi_oct", 100),
                refund("pi_oct", 900, RefundStatus.FAILED),  # failed: not counted
            ],
        }
    )

    plan = plan_credit_refunds(ACCOUNT, CUSTOMER, credit(2500), payments)

    assert placed(plan.refunds) == [
        ("create", "pi_nov", 300),
        ("create", "pi_oct", 800),
        ("create", "pi_sep", 900),
    ]
    assert (plan.leftover, plan.clear, plan.leftover_ref) == (500, 2500, "pi_nov")
    assert plan.leftover_reason == "credit balance: the recent payments are refunded in full"
    [record] = credit_records(plan, plan.refunds, NOW)
    assert (record.key, record.payment_ref, record.invoice_ref) == (
        credit_leftover_key(CUSTOMER),
        "pi_nov",
        None,
    )
    assert (record.amount, record.currency, record.created_at) == (500, "eur", NOW)
    assert str(ACCOUNT) not in repr(record)


def test_the_provider_figure_of_what_was_refunded_counts_when_it_is_larger() -> None:
    """Stripe's ``amount_refunded`` counts refunds the refund list may not show yet."""
    payments: Payments = [(payment("pi_nov", 16, refunded=700), [refund("pi_nov", 600)])]

    plan = plan_credit_refunds(ACCOUNT, CUSTOMER, credit(1000), payments)

    assert placed(plan.refunds) == [("create", "pi_nov", 200)]
    assert plan.leftover == 800


def test_fully_refunded_payments_take_nothing() -> None:
    payments: Payments = [(payment("pi_nov", 16, refunded=900), [])]

    plan = plan_credit_refunds(ACCOUNT, CUSTOMER, credit(300), payments)

    assert plan.refunds == ()
    assert (plan.leftover, plan.clear) == (300, 300)


def test_without_payments_everything_is_recorded_against_the_customer() -> None:
    plan = plan_credit_refunds(ACCOUNT, CUSTOMER, credit(1234), [])

    assert plan.refunds == ()
    assert (plan.leftover, plan.leftover_ref) == (1234, CUSTOMER)
    assert plan.leftover_reason == "credit balance: no payment of the last 366 days to refund it to"
    [record] = credit_records(plan, plan.refunds, NOW)
    assert (record.payment_ref, record.amount, record.key) == (
        CUSTOMER,
        1234,
        "cus_1:credit-balance",
    )


def test_a_balance_in_another_currency_goes_to_the_operator_in_full() -> None:
    plan = plan_credit_refunds(ACCOUNT, CUSTOMER, credit(2500, "usd"), three())

    assert plan.refunds == ()
    assert (plan.credit, plan.currency, plan.leftover, plan.clear) == (2500, "usd", 2500, 2500)
    assert plan.leftover_reason == "credit balance: the credit is in usd, the payments in eur"
    assert plan.leftover_ref == "pi_nov"


def test_only_payments_in_the_balance_currency_take_a_part() -> None:
    payments: Payments = [
        (payment("pi_usd", 1, currency="usd"), []),
        (payment("pi_eur", 30), []),
    ]

    plan = plan_credit_refunds(ACCOUNT, CUSTOMER, credit(500, "EUR"), payments)

    assert placed(plan.refunds) == [("create", "pi_eur", 500)]
    assert plan.currency == "eur"


def test_a_zero_or_owed_balance_means_nothing_to_do() -> None:
    """An erased customer is never charged: a debt is dropped with the customer."""
    for balance, reason in (
        (credit(0), "no credit balance"),
        (credit(-1500), "the customer owes money"),
        (None, "no customer at the provider"),
    ):
        plan = plan_credit_refunds(ACCOUNT, CUSTOMER, balance, three())
        assert (plan.credit, plan.refunds, plan.leftover, plan.clear) == (0, (), 0, 0)
        assert plan.reason == reason
        assert credit_records(plan, plan.refunds, NOW) == []
    assert credit_owed(None) == 0
    assert credit_owed(credit(-1500)) == 0


def test_rounding_follows_the_refund_step_of_the_currency() -> None:
    """ISK is written with two decimals and refunded in whole units: 12.34 ISK of credit on a
    payment of 10.00 refunds 10.00, the 2.34 that is left becomes a record."""
    payments: Payments = [(payment("pi_isk", 3, amount=1000, currency="isk"), [])]

    plan = plan_credit_refunds(ACCOUNT, CUSTOMER, credit(1234, "isk"), payments)

    assert placed(plan.refunds) == [("create", "pi_isk", 1000)]
    assert plan.leftover == 234


# --- retries --------------------------------------------------------------------------------


def test_a_retry_finds_the_refunds_it_made_and_makes_only_the_missing_ones() -> None:
    """The first attempt refunded November and October, then September's refund failed."""
    ours = {ref: credit_metadata(ACCOUNT, ref) for ref in ("pi_nov", "pi_oct", "pi_sep")}
    payments = three(
        refunds={
            "pi_nov": [refund("pi_nov", 900, RefundStatus.SUCCEEDED, ours["pi_nov"])],
            "pi_oct": [refund("pi_oct", 900, RefundStatus.PENDING, ours["pi_oct"])],
            "pi_sep": [refund("pi_sep", 700, RefundStatus.FAILED, ours["pi_sep"])],
        }
    )

    plan = plan_credit_refunds(ACCOUNT, CUSTOMER, credit(2500), payments)

    assert placed(plan.refunds) == [
        ("done", "pi_nov", 900),
        ("done", "pi_oct", 900),
        ("create", "pi_sep", 700),
    ]
    assert plan.refunds[2].idempotency_key == f"mowdeck-erasure-credit-{ACCOUNT}-pi_sep-1"
    assert plan.leftover == 0


def test_after_the_balance_was_zeroed_a_retry_plans_the_same_and_zeroes_nothing() -> None:
    """The balance transaction went through, deleting the customer failed: the balance is 0,
    ``cleared`` says it was 25.00. Everything is found done; nothing to zero."""
    ours = {ref: credit_metadata(ACCOUNT, ref) for ref in ("pi_nov", "pi_oct", "pi_sep")}
    payments = three(
        refunds={
            ref: [refund(ref, amount, RefundStatus.SUCCEEDED, ours[ref])]
            for ref, amount in (("pi_nov", 900), ("pi_oct", 900), ("pi_sep", 700))
        }
    )

    plan = plan_credit_refunds(ACCOUNT, CUSTOMER, credit(0, cleared=2500), payments)

    assert plan.credit == 2500
    assert [p.action for p in plan.refunds] == [RefundAction.DONE] * 3
    assert (plan.leftover, plan.clear, plan.clear_key) == (0, 0, "")


def test_after_too_many_failed_refunds_the_payment_is_skipped_and_recorded() -> None:
    ours = credit_metadata(ACCOUNT, "pi_nov")
    failed = [
        refund("pi_nov", 400 + i, RefundStatus.FAILED, ours) for i in range(MAX_FAILED_REFUNDS)
    ]
    payments: Payments = [(payment("pi_nov", 16), failed)]

    plan = plan_credit_refunds(ACCOUNT, CUSTOMER, credit(400), payments)

    [skip] = plan.refunds
    assert (skip.action, skip.amount, skip.payment_ref) == (RefundAction.SKIP, 400, "pi_nov")
    assert skip.reason == f"credit balance: {MAX_FAILED_REFUNDS} refunds failed"
    [record] = credit_records(plan, plan.refunds, NOW)
    assert (record.key, record.invoice_ref, record.amount) == ("pi_nov:", None, 400)


def test_a_refusal_for_good_is_a_skip_with_the_credit_reason() -> None:
    plan = plan_credit_refunds(ACCOUNT, CUSTOMER, credit(400), three())

    skip = credit_skipped(plan.refunds[0], "Stripe refuses (charge_disputed)")

    assert (skip.action, skip.amount) == (RefundAction.SKIP, 400)
    assert skip.reason == "credit balance: Stripe refuses (charge_disputed)"
    [record] = credit_records(plan, [skip], NOW)
    assert record.key == manual_refund_key("pi_nov", None)


def test_a_credit_refund_that_fails_later_has_the_key_of_the_erasure_record() -> None:
    """The webhook path and the erasure write one record per payment, whichever comes first."""
    ours = credit_metadata(ACCOUNT, "pi_nov")
    late = refund("pi_nov", 400, RefundStatus.FAILED, ours)

    record = lost_refund_record(late, NOW)

    assert is_erasure_metadata(ours)
    assert is_erasure_metadata(erasure_metadata(ACCOUNT, "sub_1", "in_1"))
    assert not is_erasure_metadata({"reason": "requested_by_customer"})
    assert (record.key, record.invoice_ref) == (manual_refund_key("pi_nov", None), None)
    assert record.reason.startswith("credit balance: refund ")


def test_keys_and_metadata() -> None:
    assert credit_idempotency_key(ACCOUNT, "pi_1", 0) == f"mowdeck-erasure-credit-{ACCOUNT}-pi_1"
    assert credit_idempotency_key(ACCOUNT, "pi_1", 2).endswith("-pi_1-2")
    assert clearing_idempotency_key(ACCOUNT, "cus_1", 700, "EUR").endswith("-cus_1-700eur")
    assert is_clearing_metadata(clearing_metadata(ACCOUNT))
    assert not is_clearing_metadata(erasure_metadata(ACCOUNT, "sub_1", "in_1"))
    assert earliest_payment(NOW) == NOW - timedelta(days=366)
