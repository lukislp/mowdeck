# SPDX-FileCopyrightText: 2026 Lukas Körber
# SPDX-License-Identifier: AGPL-3.0-only

"""The refund of an account erasure (ADR 0082, section 5), through the real app: Identity's
request over the in-memory bus, the fake payment provider, the memory store and, with
``MOWDECK_TEST_DATABASE_URL``, PostgreSQL.

The order at the provider is refund, cancel, delete; a refund that fails stops the erasure
before the subscription ends, a retry never refunds twice, and a refund that cannot be made is
skipped with a warning to the operator. Refund events after the erasure change nothing.
"""

from datetime import timedelta
from uuid import UUID

import pytest
import structlog
from billing_support import Harness, harness
from mowdeck_contracts.v1 import AccountEraseReply
from test_billing_db import STORES, store_of
from test_billing_erasure import erase, rows_of, subscribe

from billing.adapters.providers.fake import fake_customer_id
from billing.adapters.providers.fixtures import charge_refunded_event, refund_event
from billing.domain.errors import ProviderUnavailable, RefundImpossible
from billing.domain.models import InvoiceLine, LineKind, PaidInvoice, RefundStatus
from billing.domain.refunds import MAX_FAILED_REFUNDS, erasure_metadata


def paid(h: Harness, subscription_id: str, amount: int = 900) -> PaidInvoice:
    """The latest paid invoice of the subscription at the fake provider: a month of 30 days
    that started 10 days ago, so two thirds of it are unused (9.00 EUR: 6.00 back)."""
    start = h.clock() - timedelta(days=10)
    invoice = PaidInvoice(
        invoice_id=f"in_{subscription_id}",
        subscription_id=subscription_id,
        status="paid",
        currency="eur",
        amount_paid=amount,
        lines=(InvoiceLine(amount, LineKind.RECURRING, start, start + timedelta(days=30)),),
        payment_ref=f"pi_{subscription_id}",
        payment_amount=amount,
    )
    h.provider.paid_invoices[subscription_id] = invoice
    return invoice


async def paying(h: Harness, subscription_id: str = "sub_1", amount: int = 900) -> UUID:
    account_id = await subscribe(h, "Garden", subscription_id)
    paid(h, subscription_id, amount)
    return account_id


@pytest.mark.parametrize("kind", STORES)
async def test_the_unused_part_is_refunded_before_the_subscription_ends(
    kind: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with store_of(kind, monkeypatch) as store, harness(store=store) as h:  # type: ignore[arg-type]
        account_id = await paying(h)
        before = await rows_of(store, account_id)

        reply = await erase(h, account_id)

        assert reply == AccountEraseReply(ok=True, deleted=sum(before.values()))
        assert h.provider.calls == [
            ("refund", "pi_sub_1"),
            ("cancel_subscription", "sub_1"),
            ("delete_customer", fake_customer_id(account_id)),
        ]
        [made] = h.provider.refunds["pi_sub_1"]
        assert (made.amount, made.currency, made.status) == (600, "eur", RefundStatus.SUCCEEDED)
        assert made.metadata == erasure_metadata(account_id, "sub_1", "in_sub_1")
        assert list(h.provider.idempotency_keys) == [
            f"mowdeck-erasure-refund-{account_id}-sub_1-in_sub_1"
        ]
        assert await rows_of(store, account_id) == {}


async def test_every_paid_subscription_is_refunded_before_it_ends() -> None:
    async with harness() as h:
        account_id = await paying(h, "sub_a")
        await subscribe(h, "Second", "sub_b")
        # Move the second subscription to the first account (one account, two subscriptions).
        async with h.store.transaction() as tx:
            moved = await tx.get_subscription("fake", "sub_b")
            assert moved is not None
            moved.account_id = account_id
            await tx.save_subscription(moved)
        paid(h, "sub_b", 1500)

        assert (await erase(h, account_id)).ok

        refunds_and_cancels = [c for c in h.provider.calls if c[0] != "delete_customer"]
        assert sorted(refunds_and_cancels[0::2]) == [("refund", "pi_sub_a"), ("refund", "pi_sub_b")]
        assert sorted(refunds_and_cancels[1::2]) == [
            ("cancel_subscription", "sub_a"),
            ("cancel_subscription", "sub_b"),
        ]
        for refund_call, cancel_call in zip(
            refunds_and_cancels[0::2], refunds_and_cancels[1::2], strict=True
        ):
            assert refund_call[1] == f"pi_{cancel_call[1]}"
        assert h.provider.calls[-1][0] == "delete_customer"
        assert [r.amount for r in h.provider.refunds["pi_sub_b"]] == [1000]


async def test_a_trial_or_a_free_period_is_not_refunded() -> None:
    async with harness() as h:
        account_id = await paying(h, amount=0)

        assert (await erase(h, account_id)).ok

        assert [c[0] for c in h.provider.calls] == ["cancel_subscription", "delete_customer"]
        assert h.provider.refunds == {}


async def test_a_subscription_without_a_paid_invoice_ends_as_before() -> None:
    async with harness() as h:
        account_id = await subscribe(h, "Garden", "sub_1")

        assert (await erase(h, account_id)).ok

        assert [c[0] for c in h.provider.calls] == ["cancel_subscription", "delete_customer"]


@pytest.mark.parametrize("kind", STORES)
async def test_a_failed_refund_fails_the_erasure_and_ends_nothing(
    kind: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The refund failed: the subscription keeps running, the customer and the rows stay, and
    Identity asks again. The next attempt refunds under a new key and finishes."""
    async with store_of(kind, monkeypatch) as store, harness(store=store) as h:  # type: ignore[arg-type]
        account_id = await paying(h)
        before = await rows_of(store, account_id)
        h.provider.refund_status = RefundStatus.FAILED

        failed = await erase(h, account_id)
        calls = list(h.provider.calls)
        left = await rows_of(store, account_id)
        h.provider.refund_status = RefundStatus.PENDING
        retried = await erase(h, account_id)

        assert failed == AccountEraseReply(
            ok=False, deleted=0, error="RefundFailed: see the log of billing"
        )
        assert calls == [("refund", "pi_sub_1")]
        assert left == before
        assert retried == AccountEraseReply(ok=True, deleted=sum(before.values()))
        assert [(r.status, r.amount) for r in h.provider.refunds["pi_sub_1"]] == [
            (RefundStatus.FAILED, 600),
            (RefundStatus.PENDING, 600),
        ]
        key = f"mowdeck-erasure-refund-{account_id}-sub_1-in_sub_1"
        assert list(h.provider.idempotency_keys) == [key, f"{key}-1"]


async def test_a_refund_the_provider_could_not_take_fails_the_erasure() -> None:
    async with harness() as h:
        account_id = await paying(h)
        h.provider.failures["refund"] = ProviderUnavailable("rate limited")

        failed = await erase(h, account_id)

        assert failed == AccountEraseReply(
            ok=False, deleted=0, error="ProviderUnavailable: see the log of billing"
        )
        assert h.provider.calls == [("refund", "pi_sub_1")]
        assert h.provider.cancelled == []
        assert fake_customer_id(account_id) in h.provider.customers


async def test_a_retry_after_a_later_failure_never_refunds_twice() -> None:
    """The refund was made, then deleting the customer failed. Days later (the unused part is
    smaller by then) the retry finds the refund by its metadata and makes no second one."""
    async with harness() as h:
        account_id = await paying(h)
        customer_id = fake_customer_id(account_id)
        h.provider.failures["delete_customer"] = ProviderUnavailable("outage")
        assert not (await erase(h, account_id)).ok
        del h.provider.failures["delete_customer"]
        h.clock.advance(days=3)

        assert (await erase(h, account_id)).ok

        assert h.provider.calls == [
            ("refund", "pi_sub_1"),
            ("cancel_subscription", "sub_1"),
            ("delete_customer", customer_id),
            ("cancel_subscription", "sub_1"),
            ("delete_customer", customer_id),
        ]
        assert [r.amount for r in h.provider.refunds["pi_sub_1"]] == [600]


async def test_a_refund_refused_for_good_is_skipped_with_a_warning() -> None:
    async with harness() as h:
        account_id = await paying(h)
        h.provider.failures["refund"] = RefundImpossible("Stripe refuses (charge_disputed)")

        with structlog.testing.capture_logs() as logs:
            reply = await erase(h, account_id)

        assert reply.ok
        assert [c[0] for c in h.provider.calls] == [
            "refund",
            "cancel_subscription",
            "delete_customer",
        ]
        [warning] = [entry for entry in logs if entry["log_level"] == "warning"]
        assert warning["event"] == "refund of an erased account skipped: pay it out by hand"
        assert warning["account_id"] == str(account_id)
        assert (warning["amount"], warning["currency"]) == (600, "eur")
        assert warning["payment"] == "pi_sub_1"
        assert "charge_disputed" in warning["reason"]


async def test_after_too_many_failed_refunds_the_erasure_goes_on_with_a_warning() -> None:
    async with harness() as h:
        account_id = await paying(h)
        h.provider.refund_status = RefundStatus.FAILED
        for _ in range(MAX_FAILED_REFUNDS):
            assert not (await erase(h, account_id)).ok

        with structlog.testing.capture_logs() as logs:
            reply = await erase(h, account_id)

        assert reply.ok
        assert [c[0] for c in h.provider.calls] == [
            *["refund"] * MAX_FAILED_REFUNDS,
            "cancel_subscription",
            "delete_customer",
        ]
        [warning] = [entry for entry in logs if entry["log_level"] == "warning"]
        assert warning["reason"] == f"{MAX_FAILED_REFUNDS} refunds failed"
        assert (warning["account_id"], warning["amount"]) == (str(account_id), 600)


async def test_a_refund_that_waits_for_the_customer_counts_with_a_warning() -> None:
    async with harness() as h:
        account_id = await paying(h)
        h.provider.refund_status = RefundStatus.REQUIRES_ACTION

        with structlog.testing.capture_logs() as logs:
            reply = await erase(h, account_id)

        assert reply.ok
        [warning] = [entry for entry in logs if entry["log_level"] == "warning"]
        assert "waits for the customer" in warning["event"]
        assert warning["account_id"] == str(account_id)


# --- refund events after the erasure ---------------------------------------------------------


@pytest.mark.parametrize("kind", STORES)
async def test_late_refund_events_are_acknowledged_and_dropped(
    kind: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Stripe reports the refund (``charge.refunded``, ``refund.updated``) after the customer
    is gone. Nothing is written; a refund that failed after all is a warning for the operator."""
    async with store_of(kind, monkeypatch) as store, harness(store=store) as h:  # type: ignore[arg-type]
        account_id = await paying(h)
        assert (await erase(h, account_id)).ok
        [made] = h.provider.refunds["pi_sub_1"]
        metadata = erasure_metadata(account_id, "sub_1", "in_sub_1")
        h.clock.advance(seconds=5)

        def updated(status: str, kind: str = "refund.updated", **extra: str) -> dict[str, object]:
            return refund_event(
                kind,
                refund_id=made.id,
                payment_intent="pi_sub_1",
                amount=600,
                status=status,
                metadata=metadata,
                created=h.clock.ts,
                **extra,
            )

        charge = charge_refunded_event(
            charge_id="ch_sub_1",
            customer_id=fake_customer_id(account_id),
            payment_intent="pi_sub_1",
            amount=900,
            amount_refunded=600,
            created=h.clock.ts,
        )
        dashboard = refund_event(
            "refund.updated",
            refund_id="re_dashboard",
            payment_intent="pi_other",
            amount=100,
            status="succeeded",
            created=h.clock.ts,
        )
        with structlog.testing.capture_logs() as logs:
            outcomes = [
                (await h.webhook(event)).json()["outcome"]
                for event in (
                    charge,
                    updated("pending"),
                    updated("succeeded"),
                    dashboard,
                    updated("failed", "refund.failed", failure_reason="expired_or_canceled_card"),
                )
            ]

        assert outcomes == [
            "unknown_account",
            "unknown_account",
            "unknown_account",
            "ignored",
            "unknown_account",
        ]
        assert await rows_of(store, account_id) == {}
        [warning] = [entry for entry in logs if entry["log_level"] == "warning"]
        assert warning["event"] == "a refund of an account erasure failed: pay it out by hand"
        assert (warning["account_id"], warning["amount"]) == (str(account_id), 600)
        assert warning["failure_reason"] == "expired_or_canceled_card"
        async with store.transaction() as tx:
            row = await tx.get_event("fake", charge["id"])
        assert row is not None and row.account_id is None


async def test_a_refund_of_a_known_customer_changes_nothing() -> None:
    """A refund made in the dashboard for a customer Billing knows: acknowledged, no row."""
    async with harness() as h:
        account_id = await paying(h)
        before = await rows_of(h.store, account_id)
        charge = charge_refunded_event(
            charge_id="ch_1",
            customer_id=fake_customer_id(account_id),
            payment_intent="pi_sub_1",
            amount=900,
            amount_refunded=900,
        )

        response = await h.webhook(charge)

        assert response.json()["outcome"] == "ignored"
        after = await rows_of(h.store, account_id)
        assert after["webhook_events"] == before["webhook_events"] + 1
        assert {k: v for k, v in after.items() if k != "webhook_events"} == {
            k: v for k, v in before.items() if k != "webhook_events"
        }
