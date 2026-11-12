# SPDX-FileCopyrightText: 2026 Lukas Körber
# SPDX-License-Identifier: AGPL-3.0-only

"""The payments ledger from webhook to dashboard (ADR 0083): Stripe shaped events (the
fixtures follow the pinned API version) become anonymous facts, a repeated or out-of-order
delivery changes nothing, the erasure leaves the facts alone, and only operator admins read the
summary and the prices."""

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

from billing_support import API, PRICES, Harness, harness
from mowdeck_contracts.v1 import AccountEraseReply, AccountEraseRequest
from mowdeck_contracts.v1.subjects import account_erase_subject

from billing.adapters.db.tables import payment_facts
from billing.adapters.providers.fixtures import (
    dispute_event,
    invoice_event,
    refund_event,
    subscription_event,
)
from billing.adapters.providers.stripe_events import dispute_from, event_from, invoice_from
from billing.domain.ledger import FactKind, PaymentFact
from billing.domain.models import ManualRefund

DAY = 86400
PAYMENTS = f"{API}/operator/payments"
PRICES_URL = f"{API}/operator/prices"


def facts(h: Harness, kind: FactKind | None = None) -> list[PaymentFact]:
    found = [f for f in h.store.data.facts.values() if kind is None or f.kind is kind]
    return sorted(found, key=lambda f: (f.occurred_at, f.kind.value))


async def known(h: Harness) -> UUID:
    owner = await h.owner()
    await h.customer(owner.account_id)
    return owner.account_id


def paid(h: Harness, invoice_id: str = "in_1", **extra: Any) -> dict[str, Any]:
    now = h.clock.ts
    values: dict[str, Any] = {
        "invoice_id": invoice_id,
        "customer_id": "cus_1",
        "subscription_id": "sub_1",
        "status": "paid",
        "amount": 1178,
        "tax": 188,
        "price_id": "price_home",
        "period": (now, now + 30 * DAY),
        "created": now,
        **extra,
    }
    return invoice_event("invoice.paid", **values)


def subscription(h: Harness, status: str, account_id: UUID | None, **extra: Any) -> dict[str, Any]:
    now = h.clock.ts
    kind = extra.pop("kind", "updated")
    values: dict[str, Any] = {
        "subscription_id": "sub_1",
        "customer_id": "cus_1",
        "status": status,
        "price_id": "price_home",
        "account_id": account_id,
        "plan": "home",
        "period_start": now - DAY,
        "period_end": now + 29 * DAY,
        "created": now,
        **extra,
    }
    return subscription_event(f"customer.subscription.{kind}", **values)


# --- mapping Stripe objects ----------------------------------------------------------------


def test_stripe_invoice_and_dispute_fields_for_the_ledger() -> None:
    event = invoice_event(
        "invoice.paid",
        invoice_id="in_9",
        customer_id="cus_1",
        subscription_id="sub_1",
        status="paid",
        amount=1178,
        tax=188,
        price_id="price_home",
        period=(1_790_000_000, 1_792_592_000),
        created=1_790_000_100,
        attempt_count=2,
    )
    invoice = invoice_from(event["data"]["object"])
    assert (invoice.tax, invoice.attempt_count, invoice.price_ids) == (188, 2, ("price_home",))
    assert invoice.paid_at == datetime.fromtimestamp(1_790_000_100, UTC)
    assert invoice.period_start == datetime.fromtimestamp(1_790_000_000, UTC)
    assert invoice.period_end == datetime.fromtimestamp(1_792_592_000, UTC)
    # Without Stripe Tax and lines: no tax, no period, no prices.
    bare = invoice_from({"id": "in_0", "status": "open", "tax": None})
    assert (bare.tax, bare.period_start, bare.price_ids) == (None, None, ())
    assert invoice_from({"id": "in_0", "tax": 19}).tax == 19  # older API versions
    old_line = {
        "id": "in_0",
        "lines": {
            "data": [
                {
                    "period": {"start": 1, "end": 2},
                    "parent": {"subscription_item_details": {"proration": False}},
                    "price": {"id": "price_old"},
                }
            ]
        },
    }
    assert invoice_from(old_line).price_ids == ("price_old",)

    raw = dispute_event(
        "charge.dispute.created",
        dispute_id="dp_1",
        charge_id="ch_1",
        amount=990,
        status="needs_response",
        opened=1_790_000_000,
        due_by=1_791_000_000,
    )
    dispute = dispute_from(raw["data"]["object"])
    assert (dispute.amount, dispute.status, dispute.reason) == (990, "needs_response", "fraudulent")
    assert dispute.due_by == datetime.fromtimestamp(1_791_000_000, UTC)
    assert event_from(raw).dispute == dispute


# --- webhooks to facts ---------------------------------------------------------------------


async def test_paid_and_failed_invoices_become_facts_once() -> None:
    async with harness() as h:
        await known(h)
        event = paid(h)
        assert (await h.webhook(event)).json()["outcome"] == "applied"
        assert (await h.webhook(event)).json()["duplicate"] is True
        # Stripe sends the same object again under a new event id: still one fact.
        again = {**event, "id": "evt_resent"}
        assert (await h.webhook(again)).json()["outcome"] == "applied"
        (fact,) = facts(h, FactKind.INVOICE_PAID)
        assert (fact.currency, fact.amount, fact.tax, fact.plan_key, fact.status) == (
            "eur",
            1178,
            188,
            "home",
            "paid",
        )
        assert fact.ref is not None and "in_1" not in fact.ref

        for attempt in (1, 2, 2):
            failed = invoice_event(
                "invoice.payment_failed",
                invoice_id="in_2",
                customer_id="cus_1",
                subscription_id="sub_1",
                status="open",
                amount=990,
                attempt_count=attempt,
                event_id=f"evt_fail_{attempt}",
            )
            await h.webhook(failed)
        failures = facts(h, FactKind.PAYMENT_FAILED)
        assert [(f.attempt, f.amount) for f in failures] == [(1, 990), (2, 990)]
        # The plan of an invoice without lines comes from the subscription row, if any.
        assert {f.plan_key for f in failures} == {None}

        # An invoice of 0 (a trial) and one of a customer Billing does not know: no fact.
        await h.webhook(paid(h, "in_3", amount=0, tax=0))
        await h.webhook(paid(h, "in_4", customer_id="cus_unknown"))
        assert len(facts(h, FactKind.INVOICE_PAID)) == 1


async def test_refund_statuses_follow_the_newest_event_in_any_order() -> None:
    async with harness() as h:
        t = h.clock.ts

        def refund(status: str, created: int, event_id: str, **extra: Any) -> dict[str, Any]:
            return refund_event(
                "refund.updated",
                refund_id="re_1",
                payment_intent="pi_1",
                amount=450,
                status=status,
                created=created,
                event_id=event_id,
                **extra,
            )

        # The newer status arrives first, the older one later: the fact keeps the newer.
        assert (await h.webhook(refund("succeeded", t + 60, "evt_2"))).status_code == 200
        assert (await h.webhook(refund("pending", t, "evt_1"))).status_code == 200
        (fact,) = facts(h, FactKind.REFUND)
        assert (fact.status, fact.reason, fact.amount) == ("succeeded", "other", 450)

        # A refund of an account erasure, created then failed.
        erasure = {"reason": "account_erasure", "account_id": str(uuid4())}
        created = refund_event(
            "refund.created",
            refund_id="re_2",
            payment_intent="pi_2",
            amount=600,
            status="pending",
            metadata=erasure,
            created=t,
        )
        assert (await h.webhook(created)).json()["outcome"] == "unknown_account"
        stored = [f for f in facts(h, FactKind.REFUND) if f.reason == "erasure"]
        assert [(f.status, f.amount) for f in stored] == [("pending", 600)]


async def test_disputes_are_recorded_and_closed() -> None:
    async with harness() as h:
        t = h.clock.ts

        def dispute(kind: str, status: str, created: int) -> dict[str, Any]:
            return dispute_event(
                f"charge.dispute.{kind}",
                dispute_id="dp_1",
                charge_id="ch_1",
                amount=990,
                status=status,
                opened=t,
                due_by=t + 7 * DAY,
                created=created,
            )

        opened = await h.webhook(dispute("created", "needs_response", t))
        assert opened.json()["outcome"] == "recorded"
        await h.webhook(dispute("closed", "lost", t + 3 * DAY))
        await h.webhook(dispute("updated", "under_review", t + DAY))  # late: changes nothing
        (fact,) = facts(h, FactKind.DISPUTE)
        assert (fact.status, fact.amount, fact.reason) == ("lost", 990, "fraudulent")
        assert fact.closed_at == datetime.fromtimestamp(t + 3 * DAY, UTC)
        assert fact.due_by == datetime.fromtimestamp(t + 7 * DAY, UTC)


async def test_subscriptions_start_and_end_even_after_an_erasure() -> None:
    async with harness() as h:
        account_id = await known(h)
        await h.webhook(subscription(h, "trialing", account_id, kind="created"))
        await h.webhook(subscription(h, "active", account_id))
        (started,) = facts(h, FactKind.SUBSCRIPTION_STARTED)
        assert started.plan_key == "home"

        # The erasure removes the customer row; Stripe's deletion event comes afterwards.
        request = AccountEraseRequest(account_id=account_id, requested_at=h.clock())
        message = await h.bus.request(account_erase_subject("billing"), request, timeout=5.0)
        assert message.model(AccountEraseReply).ok
        before = len(facts(h))
        ended_at = h.clock.ts
        gone = subscription(h, "canceled", account_id, kind="deleted", ended_at=ended_at)
        assert (await h.webhook(gone)).json()["outcome"] == "unknown_account"
        (ended,) = facts(h, FactKind.SUBSCRIPTION_ENDED)
        assert (ended.plan_key, ended.occurred_at) == (
            "home",
            datetime.fromtimestamp(ended_at, UTC),
        )
        assert len(facts(h)) == before + 1
        # Nothing of the account came back.
        async with h.store.transaction() as tx:
            assert await tx.account_rows(account_id) == {}

        # A foreign subscription (no MowDeck metadata) of an unknown customer: nothing.
        foreign = subscription(h, "canceled", None, kind="deleted", subscription_id="sub_x")
        await h.webhook(foreign)
        assert len(facts(h, FactKind.SUBSCRIPTION_ENDED)) == 1


async def test_settling_a_manual_refund_counts_it_once() -> None:
    async with harness() as h:
        record = ManualRefund(
            uuid4(), "pi_9:in_9", "pi_9", "in_9", 600, "eur", "refused", h.clock()
        )
        async with h.store.transaction() as tx:
            await tx.add_manual_refund(record)
        operator = await h.operator()
        settle = f"{API}/operator/manual-refunds/{record.id}/settle"
        assert (await h.client.post(settle, headers=operator.headers)).status_code == 200
        h.clock.advance(hours=1)
        assert (await h.client.post(settle, headers=operator.headers)).status_code == 200
        (fact,) = facts(h, FactKind.REFUND)
        assert (fact.reason, fact.status, fact.amount) == ("erasure", "succeeded", 600)


async def test_old_refs_are_cleared_after_the_horizon() -> None:
    async with harness() as h:
        await known(h)
        await h.webhook(paid(h))
        assert await h.service.ledger.forget_old_refs() == 0
        h.clock.advance(days=181)
        assert await h.service.ledger.forget_old_refs() == 1
        assert [f.ref for f in facts(h)] == [None]


def test_the_ledger_table_names_nobody() -> None:
    """No account, customer, name, e-mail or provider object id: whoever adds a column here
    decides again (ADR 0083)."""
    assert {column.name for column in payment_facts.columns} == {
        "id",
        "kind",
        "ref",
        "provider",
        "occurred_at",
        "currency",
        "amount",
        "tax",
        "plan_key",
        "status",
        "reason",
        "period_start",
        "period_end",
        "attempt",
        "closed_at",
        "due_by",
        "event_at",
        "recorded_at",
        "updated_at",
    }


# --- the API -------------------------------------------------------------------------------


async def test_dashboard_and_prices_are_for_operator_admins_only() -> None:
    async with harness() as h:
        owner = await h.owner()
        operator = await h.operator()
        for url in (PAYMENTS, PRICES_URL):
            assert (await h.client.get(url)).status_code == 401
            assert (await h.client.get(url, headers=owner.headers)).status_code == 403
            assert (await h.client.get(url, headers=operator.headers)).status_code == 200
        too_many = await h.client.get(PAYMENTS, params={"months": 37}, headers=operator.headers)
        assert too_many.status_code == 422
        none = await h.client.get(PAYMENTS, params={"months": 0}, headers=operator.headers)
        assert none.status_code == 422


async def test_dashboard_summary_from_webhooks() -> None:
    start = datetime(2026, 11, 2, 8, 0, tzinfo=UTC)
    async with harness() as h:
        h.clock.now = start
        account_id = await known(h)
        await h.webhook(subscription(h, "active", account_id, kind="created"))
        await h.webhook(paid(h))
        await h.webhook(
            refund_event(
                "refund.updated",
                refund_id="re_1",
                payment_intent="pi_1",
                amount=178,
                status="succeeded",
                created=h.clock.ts,
            )
        )
        await h.webhook(
            dispute_event(
                "charge.dispute.created",
                dispute_id="dp_1",
                charge_id="ch_1",
                amount=1178,
                status="needs_response",
                opened=h.clock.ts,
                due_by=h.clock.ts + 7 * DAY,
            )
        )
        await h.webhook(
            invoice_event(
                "invoice.payment_failed",
                invoice_id="in_2",
                customer_id="cus_1",
                subscription_id="sub_1",
                status="open",
                amount=990,
                created=h.clock.ts,
            )
        )
        h.clock.advance(days=3)
        operator = await h.operator()
        body = (await h.client.get(PAYMENTS, params={"months": 2}, headers=operator.headers)).json()

    assert body["months"] == ["2026-10", "2026-11"]
    assert body["time_zone"] == "Europe/Berlin"
    assert body["since"] is not None
    assert body["currencies"] == [{"code": "eur", "exponent": 2}]
    (eur,) = body["revenue"]
    assert (eur["gross"], eur["refunded"], eur["net"]) == (1178, 178, 1000)
    assert eur["months"][1] == {
        "month": "2026-11",
        "gross": 1178,
        "tax": 188,
        "refunded": 178,
        "net": 1000,
        "invoices": 1,
        "refunds": 1,
    }
    subs = body["subscriptions"]
    assert subs["plans"] == [{"plan": "home", "trialing": 0, "active": 1, "past_due": 0}]
    # The fake provider prices every plan at 500 a month.
    assert subs["mrr"] == [{"currency": "eur", "amount": 500, "subscriptions": 1}]
    assert subs["flow"][1] == {"month": "2026-11", "started": 1, "ended": 0}
    assert body["refunds"] == [
        {"month": "2026-11", "currency": "eur", "reason": "other", "count": 1, "amount": 178}
    ]
    problems = body["problems"]
    assert problems["failures"] == [
        {"month": "2026-11", "currency": "eur", "count": 1, "amount": 990}
    ]
    assert problems["invoices"] == [
        {
            "currency": "eur",
            "open_count": 1,
            "open_amount": 990,
            "overdue_count": 1,
            "overdue_amount": 990,
        }
    ]
    assert problems["past_due_subscriptions"] == 0
    assert [d["status"] for d in problems["open_disputes"]] == ["needs_response"]
    assert problems["closed_disputes"] == []
    # No id of any kind in the answer.
    text = str(body)
    for needle in ("cus_1", "sub_1", "in_1", "in_2", "dp_1", "ch_1", "re_1", str(account_id)):
        assert needle not in text


async def test_prices_list_every_configured_plan_read_only() -> None:
    async with harness() as h:
        h.provider.amounts["home"] = 9900
        operator = await h.operator()
        body = (await h.client.get(PRICES_URL, headers=operator.headers)).json()
    assert body["currency"] == PRICES.currency
    by_plan = {p["plan"]: p for p in body["prices"]}
    assert set(by_plan) == set(PRICES.sold)
    assert by_plan["home"] == {
        "plan": "home",
        "price_id": "price_home",
        "currency": "eur",
        "unit_amount": 9900,
        "interval": "month",
        "interval_count": 1,
        "exponent": 2,
        "monthly_amount": 9900,
    }


async def test_empty_ledger_answers_an_empty_dashboard() -> None:
    async with harness() as h:
        h.clock.now = datetime(2026, 11, 12, tzinfo=UTC) + timedelta(hours=1)
        operator = await h.operator()
        body = (await h.client.get(PAYMENTS, headers=operator.headers)).json()
    assert len(body["months"]) == 12
    assert body["since"] is None
    assert body["revenue"] == [] and body["refunds"] == [] and body["currencies"] == []
    assert body["subscriptions"]["plans"] == [] and body["subscriptions"]["mrr"] == []
    assert all(f["started"] == 0 and f["ended"] == 0 for f in body["subscriptions"]["flow"])
