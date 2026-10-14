"""Webhooks: signature, exactly-once, and the status mapping to Entitlements (fake bus)."""

from datetime import timedelta
from typing import Any
from uuid import UUID, uuid4

from billing_support import SECRET, WEBHOOK, Harness, harness
from mowdeck_contracts.v1 import (
    BILLING_SUBSCRIPTION_CHANGED,
    EventEnvelope,
    PlanStatus,
    SubscriptionChanged,
    SubscriptionStatus,
)
from mowdeck_contracts.v1.subjects import event_subject_for_type

from billing.adapters.providers.fixtures import (
    checkout_completed_event,
    encode,
    invoice_event,
    sign,
    subscription_event,
)
from billing.domain.models import EventOutcome, ProviderSubscription

DAY = 86400


def _sub(h: Harness, account_id: UUID | None, status: str, **extra: Any) -> dict[str, Any]:
    now = h.clock.ts
    values: dict[str, Any] = {
        "subscription_id": "sub_1",
        "customer_id": "cus_1",
        "status": status,
        "price_id": "price_home",
        "account_id": account_id,
        "period_start": now - DAY,
        "period_end": now + 29 * DAY,
        "created": now,
        **extra,
    }
    return subscription_event("customer.subscription.updated", **values)


def _changes(h: Harness) -> list[SubscriptionChanged]:
    messages = h.bus.messages(event_subject_for_type(BILLING_SUBSCRIPTION_CHANGED))
    return [m.model(EventEnvelope[SubscriptionChanged]).payload for m in messages]


async def test_billing_webhook_rejects_missing_and_invalid_signatures() -> None:
    async with harness() as h:
        event = _sub(h, uuid4(), "active")
        body = encode(event)
        missing = await h.client.post(WEBHOOK, content=body)
        assert missing.status_code == 400
        assert missing.json()["type"] == "urn:mowdeck:problem:invalid-signature"
        wrong = await h.webhook(event, secret="whsec_other")  # noqa: S106 - test value
        assert wrong.status_code == 400
        old = await h.client.post(
            WEBHOOK, content=body, headers={"Stripe-Signature": sign(body, SECRET, 1_000_000)}
        )
        assert old.status_code == 400  # outside the 300 s tolerance
        tampered = await h.client.post(
            WEBHOOK,
            content=body.replace(b"active", b"canceled"),
            headers={"Stripe-Signature": sign(body, SECRET)},
        )
        assert tampered.status_code == 400
        assert h.entitlements.requests == []
        async with h.store.transaction() as tx:
            assert await tx.get_event("fake", event["id"]) is None


async def test_billing_webhook_without_configured_secret_refuses_everything() -> None:
    async with harness(billing_stripe_webhook_secret=None) as h:
        h.provider.webhook_secret = ""
        response = await h.webhook(_sub(h, uuid4(), "active"))
        assert response.status_code == 400


async def test_billing_webhook_replayed_event_is_processed_once() -> None:
    async with harness() as h:
        account_id = uuid4()
        event = _sub(h, account_id, "active")
        first = await h.webhook(event)
        assert first.status_code == 200, first.text
        assert first.json() == {
            "received": True,
            "event_id": event["id"],
            "outcome": "applied",
            "duplicate": False,
        }
        again = await h.webhook(event)
        assert again.status_code == 200
        assert again.json()["duplicate"] is True
        assert again.json()["outcome"] == "applied"
        assert len(h.entitlements.requests) == 1
        async with h.store.transaction() as tx:
            stored = await tx.get_event("fake", event["id"])
        assert stored is not None
        assert (stored.attempts, stored.account_id) == (1, account_id)


async def test_billing_past_due_sets_grace_and_active_restores() -> None:
    async with harness(billing_grace_days=14) as h:
        account_id = uuid4()
        period_start = h.clock.ts - DAY
        response = await h.webhook(_sub(h, account_id, "past_due", period_start=period_start))
        assert response.status_code == 200, response.text
        grace = h.entitlements.requests[-1]
        assert (grace.account_id, grace.plan, grace.status) == (
            account_id,
            "home",
            PlanStatus.GRACE,
        )
        # Grace runs from the end of the last paid period (the unpaid period's start).
        assert grace.grace_until == h.clock.now - timedelta(days=1) + timedelta(days=14)
        assert grace.reference == "sub_1"
        assert "past_due" in grace.reason

        h.clock.advance(hours=2)
        restored = await h.webhook(_sub(h, account_id, "active"))
        assert restored.status_code == 200
        active = h.entitlements.requests[-1]
        assert (active.status, active.grace_until) == (PlanStatus.ACTIVE, None)

        summary = await h.service.summary(account_id)
        assert summary.subscription is not None
        assert summary.subscription.status is SubscriptionStatus.ACTIVE
        assert summary.subscription.plan_status is PlanStatus.ACTIVE
        assert summary.has_customer  # remembered from the subscription's customer
        changes = _changes(h)
        assert [c.plan_status for c in changes] == [PlanStatus.GRACE, PlanStatus.ACTIVE]
        assert changes[0].event_type == "customer.subscription.updated"


async def test_billing_status_mapping_trial_and_ended_subscriptions() -> None:
    async with harness(billing_grace_days=10) as h:
        trial_account, ended_account, old_account = uuid4(), uuid4(), uuid4()
        trial_end = h.clock.ts + 14 * DAY
        await h.webhook(
            _sub(h, trial_account, "trialing", subscription_id="sub_t", trial_end=trial_end)
        )
        trial = h.entitlements.requests[-1]
        assert (trial.status, int(trial.trial_until.timestamp())) == (  # type: ignore[union-attr]
            PlanStatus.TRIAL,
            trial_end,
        )
        # Canceled at the period end, still inside the grace days: grace until end + 10 days.
        ended_at = h.clock.ts - 2 * DAY
        await h.webhook(
            _sub(h, ended_account, "canceled", subscription_id="sub_c", ended_at=ended_at)
        )
        ended = h.entitlements.requests[-1]
        assert ended.status is PlanStatus.GRACE
        assert int(ended.grace_until.timestamp()) == ended_at + 10 * DAY  # type: ignore[union-attr]
        # Ended long ago: expired.
        await h.webhook(
            _sub(
                h,
                old_account,
                "unpaid",
                subscription_id="sub_o",
                period_start=h.clock.ts - 60 * DAY,
            )
        )
        assert h.entitlements.requests[-1].status is PlanStatus.EXPIRED
        # incomplete: the first payment is pending, the plan stays as it is.
        before = len(h.entitlements.requests)
        response = await h.webhook(_sub(h, uuid4(), "incomplete", subscription_id="sub_i"))
        assert response.json()["outcome"] == "applied"
        assert len(h.entitlements.requests) == before


async def test_billing_out_of_order_event_is_stale() -> None:
    async with harness() as h:
        account_id = uuid4()
        newer = _sub(h, account_id, "active", created=h.clock.ts)
        older = _sub(h, account_id, "past_due", created=h.clock.ts - 60)
        assert (await h.webhook(newer)).json()["outcome"] == "applied"
        stale = await h.webhook(older)
        assert stale.json()["outcome"] == "stale"
        assert [r.status for r in h.entitlements.requests] == [PlanStatus.ACTIVE]


async def test_billing_unknown_account_and_ignored_types() -> None:
    async with harness() as h:
        orphan = _sub(h, None, "active", customer_id="cus_unknown")
        assert (await h.webhook(orphan)).json()["outcome"] == "unknown_account"
        other = {**_sub(h, uuid4(), "active"), "type": "customer.created"}
        assert (await h.webhook(other)).json()["outcome"] == "ignored"
        invoice = invoice_event(
            "invoice.paid",
            invoice_id="in_x",
            customer_id="cus_unknown",
            subscription_id="sub_x",
            status="paid",
            amount=500,
        )
        assert (await h.webhook(invoice)).json()["outcome"] == "unknown_account"
        trial_end = {
            **_sub(h, None, "trialing", customer_id="cus_unknown"),
            "type": "customer.subscription.trial_will_end",
        }
        assert (await h.webhook(trial_end)).json()["outcome"] == "unknown_account"
        assert h.entitlements.requests == []


async def test_billing_entitlements_timeout_answers_500_and_the_retry_applies() -> None:
    async with harness(entitlements_timeout=0.05) as h:
        account_id = uuid4()
        event = _sub(h, account_id, "past_due")
        h.entitlements.silent = True
        failed = await h.webhook(event)
        assert failed.status_code == 500
        async with h.store.transaction() as tx:
            stored = await tx.get_event("fake", event["id"])
        assert stored is not None
        assert (stored.outcome, stored.processed_at, stored.claimed_at) == (
            EventOutcome.FAILED,
            None,
            None,
        )
        h.entitlements.silent = False
        retried = await h.webhook(event)
        assert retried.status_code == 200
        assert retried.json()["outcome"] == "applied"
        assert h.entitlements.requests[-1].status is PlanStatus.GRACE


async def test_billing_rejected_plan_and_superseded_old_subscription() -> None:
    async with harness() as h:
        account_id = uuid4()
        h.entitlements.known_plans = {"fleet"}
        rejected = await h.webhook(_sub(h, account_id, "active", subscription_id="sub_old"))
        assert rejected.json()["outcome"] == "rejected"
        h.entitlements.known_plans = {"home", "fleet"}
        await h.webhook(
            _sub(h, account_id, "active", subscription_id="sub_new", price_id="price_fleet")
        )
        assert h.entitlements.requests[-1].plan == "fleet"
        count = len(h.entitlements.requests)
        # A late cancellation of the old subscription does not restrict the account.
        h.clock.advance(minutes=1)
        late = await h.webhook(_sub(h, account_id, "canceled", subscription_id="sub_old"))
        assert late.json()["outcome"] == "applied"
        assert len(h.entitlements.requests) == count
        # A subscription whose price and metadata name no plan cannot be set.
        unknown = await h.webhook(
            subscription_event(
                "customer.subscription.created",
                subscription_id="sub_np",
                customer_id="cus_np",
                status="active",
                price_id="price_unknown",
                account_id=uuid4(),
                period_start=h.clock.ts,
                period_end=h.clock.ts + DAY,
            )
        )
        assert unknown.json()["outcome"] == "rejected"


async def test_billing_checkout_completed_fetches_the_subscription() -> None:
    async with harness() as h:
        account_id = uuid4()
        h.provider.subscriptions["sub_ck"] = ProviderSubscription(
            id="sub_ck",
            customer_id="cus_ck",
            status=SubscriptionStatus.TRIALING,
            price_id="price_fleet",
            trial_end=h.clock.now + timedelta(days=14),
            current_period_end=h.clock.now + timedelta(days=14),
        )
        event = checkout_completed_event(
            session_id="cs_1",
            customer_id="cus_ck",
            subscription_id="sub_ck",
            account_id=account_id,
            plan="fleet",
            created=h.clock.ts,
        )
        response = await h.webhook(event)
        assert response.json()["outcome"] == "applied", response.text
        request = h.entitlements.requests[-1]
        assert (request.account_id, request.plan, request.status) == (
            account_id,
            "fleet",
            PlanStatus.TRIAL,
        )
        async with h.store.transaction() as tx:
            customer = await tx.get_customer(account_id, "fake")
        assert customer is not None
        assert customer.provider_customer_id == "cus_ck"
        # A checkout without subscription (unusual) only remembers the customer.
        bare = checkout_completed_event(
            session_id="cs_2",
            customer_id="cus_2",
            subscription_id=None,
            account_id=uuid4(),
            plan="home",
        )
        assert (await h.webhook(bare)).json()["outcome"] == "applied"
        orphan = checkout_completed_event(
            session_id="cs_3",
            customer_id="cus_3",
            subscription_id=None,
            account_id=uuid4(),
            plan="x",
        )
        orphan["data"]["object"]["client_reference_id"] = None
        orphan["data"]["object"]["metadata"] = {}
        assert (await h.webhook(orphan)).json()["outcome"] == "unknown_account"


async def test_billing_invoices_are_stored_and_listed_per_account() -> None:
    async with harness() as h:
        owner = await h.owner()
        await h.webhook(_sub(h, owner.account_id, "active", customer_id="cus_inv"))
        for number, (kind, status) in enumerate(
            [("invoice.payment_failed", "open"), ("invoice.paid", "paid")]
        ):
            event = invoice_event(
                kind,
                invoice_id=f"in_{number}",
                customer_id="cus_inv",
                subscription_id="sub_1",
                status=status,
                amount=900,
                created=h.clock.ts + number,
            )
            assert (await h.webhook(event)).json()["outcome"] == "applied"
        # The same invoice updated (paid after a failure) stays one row.
        update = invoice_event(
            "invoice.paid",
            invoice_id="in_0",
            customer_id="cus_inv",
            subscription_id="sub_1",
            status="paid",
            amount=900,
            created=h.clock.ts,
        )
        await h.webhook(update)
        response = await h.client.get("/api/v1/billing/invoices", headers=owner.headers)
        assert response.status_code == 200
        invoices = response.json()
        assert [i["status"] for i in invoices] == ["paid", "paid"]
        assert invoices[0]["amount_paid"] == 900
        assert invoices[0]["hosted_url"].startswith("https://invoice.stripe.com/")
        assert invoices[0]["pdf_url"].endswith("/pdf")
        other = await h.owner()
        foreign = await h.client.get("/api/v1/billing/invoices", headers=other.headers)
        assert foreign.json() == []


async def test_billing_trial_will_end_is_published_without_plan_change() -> None:
    async with harness() as h:
        account_id = uuid4()
        event = {
            **_sub(h, account_id, "trialing", trial_end=h.clock.ts + 3 * DAY),
            "type": "customer.subscription.trial_will_end",
        }
        assert (await h.webhook(event)).json()["outcome"] == "applied"
        assert h.entitlements.requests == []
        change = _changes(h)[-1]
        assert (change.event_type, change.plan, change.plan_status) == (
            "customer.subscription.trial_will_end",
            "home",
            None,
        )


async def test_billing_cancel_open_subscriptions() -> None:
    async with harness() as h:
        account_id = uuid4()
        h.provider.subscriptions["sub_1"] = ProviderSubscription(
            id="sub_1", customer_id="cus_1", status=SubscriptionStatus.ACTIVE
        )
        await h.webhook(_sub(h, account_id, "active"))
        assert await h.service.cancel(account_id) == ["sub_1"]
        assert h.provider.cancelled == ["sub_1"]
