"""Domain: status rules, the price file, the Stripe object mapping and the fixture signature."""

import json
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from mowdeck_contracts.v1 import AccountKind, PlanStatus, SubscriptionStatus

from billing.adapters.providers.fixtures import (
    checkout_completed_event,
    encode,
    invoice_event,
    sign,
    subscription_event,
)
from billing.adapters.providers.stripe import verify_stripe_payload
from billing.adapters.providers.stripe_events import event_from, invoice_from, subscription_from
from billing.domain.errors import InvalidInput, InvalidSignature
from billing.domain.models import BillingAccount
from billing.domain.prices import PriceTable, parse_overrides, parse_price_file
from billing.domain.rules import plan_decision

NOW = datetime(2026, 10, 10, 8, 0, tzinfo=UTC)
DAY = timedelta(days=1)


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (SubscriptionStatus.TRIALING, PlanStatus.TRIAL),
        (SubscriptionStatus.ACTIVE, PlanStatus.ACTIVE),
        (SubscriptionStatus.PAST_DUE, PlanStatus.GRACE),
        (SubscriptionStatus.UNPAID, PlanStatus.GRACE),
        (SubscriptionStatus.CANCELED, PlanStatus.GRACE),
        (SubscriptionStatus.INCOMPLETE_EXPIRED, PlanStatus.GRACE),
        (SubscriptionStatus.PAUSED, PlanStatus.GRACE),
    ],
)
def test_billing_rules_map_recent_statuses(
    status: SubscriptionStatus, expected: PlanStatus
) -> None:
    decision = plan_decision(
        status,
        now=NOW,
        grace_days=14,
        current_period_start=NOW - DAY,
        current_period_end=NOW + 29 * DAY,
        trial_end=NOW + 3 * DAY,
    )
    assert decision is not None
    assert decision.status is expected


def test_billing_rules_grace_anchors_and_expiry() -> None:
    past_due = plan_decision(
        SubscriptionStatus.PAST_DUE,
        now=NOW,
        grace_days=14,
        current_period_start=NOW - DAY,
        current_period_end=NOW + 29 * DAY,
    )
    assert past_due is not None
    assert past_due.grace_until == NOW + 13 * DAY  # from the paid period's end
    canceled = plan_decision(
        SubscriptionStatus.CANCELED,
        now=NOW,
        grace_days=14,
        current_period_end=NOW + 10 * DAY,
        ended_at=NOW - 2 * DAY,
    )
    assert canceled is not None
    assert canceled.grace_until == NOW + 12 * DAY
    long_ago = plan_decision(
        SubscriptionStatus.CANCELED, now=NOW, grace_days=14, current_period_end=NOW - 30 * DAY
    )
    assert long_ago is not None
    assert (long_ago.status, long_ago.grace_until) == (PlanStatus.EXPIRED, None)
    # Past due stays grace even when the period start is long ago (the provider still retries).
    late = plan_decision(
        SubscriptionStatus.PAST_DUE, now=NOW, grace_days=1, current_period_start=NOW - 30 * DAY
    )
    assert late is not None
    assert late.status is PlanStatus.GRACE
    no_dates = plan_decision(SubscriptionStatus.UNPAID, now=NOW, grace_days=3)
    assert no_dates is not None
    assert no_dates.grace_until == NOW + 3 * DAY
    assert plan_decision(SubscriptionStatus.INCOMPLETE, now=NOW, grace_days=14) is None


def test_billing_price_file_parsing_and_overrides() -> None:
    table = parse_price_file("currency: usd\nprices:\n  home: price_1\n  fleet: ''\n")
    assert table == PriceTable(currency="usd", prices={"home": "price_1", "fleet": ""})
    assert table.sold == {"home": "price_1"}
    assert (table.price_for("home"), table.price_for("fleet"), table.price_for("x")) == (
        "price_1",
        None,
        None,
    )
    assert (table.plan_for("price_1"), table.plan_for("price_9"), table.plan_for(None)) == (
        "home",
        None,
        None,
    )
    merged = table.with_overrides(" fleet=price_2 , ,home= ")
    assert merged.sold == {"fleet": "price_2"}
    assert parse_price_file("") == PriceTable()
    assert parse_overrides("") == {}


@pytest.mark.parametrize(
    "text",
    [
        "prices: [1]",
        "- a",
        "currency: EURO",
        "prices:\n  Home: price_1",
        "prices:\n  home: 'price 1'",
        "prices: {home: [1]}",
        "prices: {home: 5}",
        ": : :",
    ],
)
def test_billing_price_file_rejects_invalid_content(text: str) -> None:
    with pytest.raises(InvalidInput):
        parse_price_file(text)


def test_billing_price_overrides_reject_garbage() -> None:
    with pytest.raises(InvalidInput):
        parse_overrides("home")


def test_billing_account_business_details() -> None:
    assert BillingAccount(uuid4()).collects_business_details
    assert BillingAccount(uuid4(), kind=AccountKind.BUSINESS).collects_business_details
    assert not BillingAccount(uuid4(), kind=AccountKind.HOUSEHOLD).collects_business_details


def test_billing_stripe_subscription_mapping_new_and_old_layout() -> None:
    account_id = uuid4()
    event = subscription_event(
        "customer.subscription.updated",
        subscription_id="sub_1",
        customer_id="cus_1",
        status="past_due",
        price_id="price_home",
        account_id=account_id,
        plan="home",
        period_start=1_000,
        period_end=2_000,
        trial_end=1_500,
        cancel_at_period_end=True,
        created=1_234,
    )
    parsed = event_from(event)
    assert (parsed.type, parsed.created) == (
        "customer.subscription.updated",
        datetime.fromtimestamp(1_234, UTC),
    )
    sub = parsed.subscription
    assert sub is not None
    assert (sub.id, sub.customer_id, sub.status, sub.price_id) == (
        "sub_1",
        "cus_1",
        SubscriptionStatus.PAST_DUE,
        "price_home",
    )
    assert (sub.account_id, sub.plan_key, sub.cancel_at_period_end) == (account_id, "home", True)
    assert sub.current_period_end == datetime.fromtimestamp(2_000, UTC)
    assert sub.current_period_start == datetime.fromtimestamp(1_000, UTC)
    # Pre-basil layout (period on the subscription), expanded customer, odd values.
    old = subscription_from(
        {
            "id": "sub_2",
            "status": "something_new",
            "customer": {"id": "cus_2"},
            "current_period_end": 3_000,
            "metadata": {"account_id": "not-a-uuid"},
            "items": {"data": []},
        }
    )
    assert (old.customer_id, old.status, old.account_id, old.price_id) == (
        "cus_2",
        SubscriptionStatus.INCOMPLETE,
        None,
        None,
    )
    assert old.current_period_end == datetime.fromtimestamp(3_000, UTC)


def test_billing_stripe_invoice_and_checkout_mapping() -> None:
    event = event_from(
        invoice_event(
            "invoice.payment_failed",
            invoice_id="in_1",
            customer_id="cus_1",
            subscription_id="sub_1",
            status="open",
            amount=1200,
        )
    )
    invoice = event.invoice
    assert invoice is not None
    assert (invoice.subscription_id, invoice.amount_due, invoice.amount_paid) == ("sub_1", 1200, 0)
    legacy = invoice_from({"id": "in_2", "subscription": "sub_9", "parent": None})
    assert (legacy.subscription_id, legacy.status, legacy.currency) == ("sub_9", "unknown", "")
    account_id = uuid4()
    checkout = event_from(
        checkout_completed_event(
            session_id="cs_1",
            customer_id="cus_1",
            subscription_id="sub_1",
            account_id=account_id,
            plan="fleet",
        )
    ).checkout
    assert checkout is not None
    assert (checkout.account_id, checkout.plan_key, checkout.subscription_id) == (
        account_id,
        "fleet",
        "sub_1",
    )
    bare = event_from({"id": "evt_1", "type": "ping", "data": None})
    assert (bare.subscription, bare.invoice, bare.checkout) == (None, None, None)


def test_billing_fixture_signature_verifies_like_stripe() -> None:
    body = encode({"id": "evt_1", "object": "event", "type": "x", "data": {"object": {}}})
    header = sign(body, "whsec_a")
    assert verify_stripe_payload(body, header, "whsec_a")["id"] == "evt_1"
    for payload, signature, secret in [
        (body, header, "whsec_b"),
        (body, None, "whsec_a"),
        (body, header, ""),
        (body, "garbage", "whsec_a"),
        (b"\xff\xfe", sign(b"\xff\xfe", "whsec_a"), "whsec_a"),
    ]:
        with pytest.raises(InvalidSignature):
            verify_stripe_payload(payload, signature, secret)
    not_event = json.dumps([1]).encode()
    with pytest.raises(InvalidSignature):
        verify_stripe_payload(not_event, sign(not_event, "whsec_a"), "whsec_a")
