# SPDX-FileCopyrightText: 2026 Lukas Körber
# SPDX-License-Identifier: AGPL-3.0-only

"""The refund of an account erasure (ADR 0082, section 5), through the real app: Identity's
request over the in-memory bus, the fake payment provider, the memory store and, with
``MOWDECK_TEST_DATABASE_URL``, PostgreSQL.

The order at the provider is refund (every paid invoice that bills time containing now), cancel,
delete; a refund that fails stops the erasure before the subscription ends, a retry never
refunds twice, and a refund that cannot be made is skipped and recorded for the operator in the
transaction that deletes the rows. Refund events after the erasure write no row of the account;
one of a refund that failed after the erasure is recorded for the operator once.
"""

from datetime import timedelta
from uuid import UUID

import pytest
import structlog
from billing_support import API, Harness, harness
from mowdeck_contracts.v1 import AccountEraseReply
from prometheus_client import CollectorRegistry
from test_billing_db import STORES, store_of
from test_billing_erasure import erase, rows_of, subscribe

from billing.adapters.memory import MemoryTx
from billing.adapters.metrics import BillingMetrics
from billing.adapters.providers.fake import fake_customer_id
from billing.adapters.providers.fixtures import charge_refunded_event, refund_event
from billing.application.ports import Store
from billing.domain.errors import ProviderUnavailable, RefundImpossible
from billing.domain.models import (
    InvoiceLine,
    LineKind,
    ManualRefund,
    PaidInvoice,
    ProviderRefund,
    RefundStatus,
)
from billing.domain.refunds import MAX_FAILED_REFUNDS, erasure_metadata


def paid(h: Harness, subscription_id: str, amount: int = 900) -> PaidInvoice:
    """A paid invoice of the subscription at the fake provider: a month of 30 days that started
    10 days ago, so two thirds of it are unused (9.00 EUR: 6.00 back)."""
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
    h.provider.paid_invoices.setdefault(subscription_id, []).append(invoice)
    return invoice


def upgraded(h: Harness, subscription_id: str = "sub_1") -> PaidInvoice:
    """An upgrade 5 days ago (25 days of the month left) billed on its own invoice: a credit of
    -7.50 for the old plan's rest (9.00 x 25/30) and +15.00 for the new plan's rest (18.00 x
    25/30), 7.50 paid. 20 of its 25 days are unused: 7.50 x 20/25 = 6.00. With the month's own
    invoice (6.00) the refund is 12.00, the new plan's unused 20 days (18.00 x 20/30)."""
    end = h.clock() + timedelta(days=20)
    since = h.clock() - timedelta(days=5)
    invoice = PaidInvoice(
        invoice_id=f"in_{subscription_id}_up",
        subscription_id=subscription_id,
        status="paid",
        currency="eur",
        amount_paid=750,
        lines=(
            InvoiceLine(-750, LineKind.PRORATION, since, end),
            InvoiceLine(1500, LineKind.PRORATION, since, end),
        ),
        payment_ref=f"pi_{subscription_id}_up",
        payment_amount=750,
    )
    h.provider.paid_invoices.setdefault(subscription_id, []).insert(0, invoice)
    return invoice


async def records(store: Store, *, settled: bool = False) -> list[ManualRefund]:
    async with store.transaction() as tx:
        return await tx.manual_refunds(settled=settled)


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


@pytest.mark.parametrize("kind", STORES)
async def test_an_upgrade_billed_on_its_own_invoice_is_refunded_too(
    kind: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Both paid invoices that bill time containing now, each against its own payment, under
    its own key; then the cancellation."""
    async with store_of(kind, monkeypatch) as store, harness(store=store) as h:  # type: ignore[arg-type]
        account_id = await paying(h)
        upgraded(h)

        assert (await erase(h, account_id)).ok

        assert h.provider.calls == [
            ("refund", "pi_sub_1_up"),
            ("refund", "pi_sub_1"),
            ("cancel_subscription", "sub_1"),
            ("delete_customer", fake_customer_id(account_id)),
        ]
        assert [r.amount for r in h.provider.refunds["pi_sub_1_up"]] == [600]
        assert [r.amount for r in h.provider.refunds["pi_sub_1"]] == [600]
        assert h.provider.refunds["pi_sub_1_up"][0].metadata == erasure_metadata(
            account_id, "sub_1", "in_sub_1_up"
        )
        assert sorted(h.provider.idempotency_keys) == sorted(
            f"mowdeck-erasure-refund-{account_id}-sub_1-{invoice}"
            for invoice in ("in_sub_1", "in_sub_1_up")
        )


async def test_a_retry_after_the_second_invoice_failed_refunds_only_that_one() -> None:
    """The upgrade's refund went through, the month's failed: the erasure fails before the
    cancellation. The retry finds the first refund and makes only the second again."""
    async with harness() as h:
        account_id = await paying(h)
        upgraded(h)
        statuses = iter([RefundStatus.SUCCEEDED, RefundStatus.FAILED, RefundStatus.PENDING])
        original = h.provider.refund

        async def refund_with(*args: object, **kwargs: object) -> ProviderRefund:
            h.provider.refund_status = next(statuses)
            return await original(*args, **kwargs)  # type: ignore[arg-type]

        h.provider.refund = refund_with  # type: ignore[method-assign]

        first = await erase(h, account_id)
        second = await erase(h, account_id)

        assert not first.ok
        assert second.ok
        assert h.provider.calls == [
            ("refund", "pi_sub_1_up"),
            ("refund", "pi_sub_1"),
            ("refund", "pi_sub_1"),
            ("cancel_subscription", "sub_1"),
            ("delete_customer", fake_customer_id(account_id)),
        ]
        assert [(r.status, r.amount) for r in h.provider.refunds["pi_sub_1_up"]] == [
            (RefundStatus.SUCCEEDED, 600)
        ]
        assert [(r.status, r.amount) for r in h.provider.refunds["pi_sub_1"]] == [
            (RefundStatus.FAILED, 600),
            (RefundStatus.PENDING, 600),
        ]


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


@pytest.mark.parametrize("kind", STORES)
async def test_a_refund_refused_for_good_is_skipped_recorded_and_warned(
    kind: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with store_of(kind, monkeypatch) as store, harness(store=store) as h:  # type: ignore[arg-type]
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
        assert await rows_of(store, account_id) == {}
        [record] = await records(store)
        assert (record.payment_ref, record.invoice_ref) == ("pi_sub_1", "in_sub_1")
        assert (record.amount, record.currency) == (600, "eur")
        assert "charge_disputed" in record.reason
        assert (record.created_at, record.settled_at) == (h.clock(), None)
        assert str(account_id) not in repr(record)
        [warning] = [entry for entry in logs if entry["log_level"] == "warning"]
        assert (
            warning["event"]
            == "refund of an erased account skipped: recorded to pay it out by hand"
        )
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
        [record] = await records(h.store)
        assert (record.amount, record.reason) == (600, f"{MAX_FAILED_REFUNDS} refunds failed")


async def test_the_record_is_written_with_the_deletion_of_the_rows_or_not_at_all(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One transaction: when deleting the rows fails, there is no record either, and the retry
    (which skips again) writes it once with the deletion."""

    async def purge_fails(self: MemoryTx, account_id: UUID) -> int:
        del self, account_id
        raise RuntimeError("database gone")

    async with harness() as h:
        account_id = await paying(h)
        before = await rows_of(h.store, account_id)
        h.provider.failures["refund"] = RefundImpossible("Stripe refuses (charge_disputed)")
        with monkeypatch.context() as patched:
            patched.setattr(MemoryTx, "purge_account", purge_fails)
            failed = await erase(h, account_id)
        left = await rows_of(h.store, account_id)
        none = await records(h.store)

        retried = await erase(h, account_id)

        assert not failed.ok
        assert left == before
        assert none == []
        assert retried.ok
        assert [r.amount for r in await records(h.store)] == [600]
        assert await rows_of(h.store, account_id) == {}


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

        again = updated("failed", "refund.updated", failure_reason="expired_or_canceled_card")
        outcomes.append((await h.webhook(again)).json()["outcome"])

        assert outcomes == [
            "unknown_account",
            "unknown_account",
            "unknown_account",
            "ignored",
            "unknown_account",
            "unknown_account",
        ]
        assert await rows_of(store, account_id) == {}
        [warning] = [entry for entry in logs if entry["log_level"] == "warning"]
        assert warning["event"] == "a refund of an account erasure failed: pay it out by hand"
        assert (warning["account_id"], warning["amount"]) == (str(account_id), 600)
        assert warning["failure_reason"] == "expired_or_canceled_card"
        # Recorded once, for the payment and the invoice; the second report adds nothing.
        [record] = await records(store)
        assert (record.payment_ref, record.invoice_ref, record.amount) == (
            "pi_sub_1",
            "in_sub_1",
            600,
        )
        assert record.reason == f"refund {made.id} failed (expired_or_canceled_card)"
        async with store.transaction() as tx:
            row = await tx.get_event("fake", charge["id"])
        assert row is not None and row.account_id is None


async def test_a_refund_that_fails_while_the_erasure_runs_is_not_recorded() -> None:
    """The account still has its rows: the next attempt of the erasure refunds again (or skips
    and records), so the webhook records nothing."""
    async with harness() as h:
        account_id = await paying(h)
        failed = refund_event(
            "refund.failed",
            refund_id="re_early",
            payment_intent="pi_sub_1",
            amount=600,
            status="failed",
            metadata=erasure_metadata(account_id, "sub_1", "in_sub_1"),
        )

        response = await h.webhook(failed)

        assert response.json()["outcome"] == "unknown_account"
        assert await records(h.store) == []


async def test_a_skipped_refund_and_its_late_failure_make_one_record() -> None:
    async with harness() as h:
        account_id = await paying(h)
        h.provider.refund_status = RefundStatus.FAILED
        for _ in range(MAX_FAILED_REFUNDS):
            assert not (await erase(h, account_id)).ok
        assert (await erase(h, account_id)).ok
        late = h.provider.refunds["pi_sub_1"][0]

        response = await h.webhook(
            refund_event(
                "refund.failed",
                refund_id=late.id,
                payment_intent="pi_sub_1",
                amount=600,
                status="failed",
                metadata=late.metadata,
            )
        )

        assert response.json()["outcome"] == "unknown_account"
        assert [r.reason for r in await records(h.store)] == [
            f"{MAX_FAILED_REFUNDS} refunds failed"
        ]


# --- the operator's side: list, settle, sweep -------------------------------------------------


@pytest.mark.parametrize("kind", STORES)
async def test_records_are_listed_settled_and_deleted_after_the_retention(
    kind: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with store_of(kind, monkeypatch) as store, harness(store=store) as h:  # type: ignore[arg-type]
        book = h.service.manual_refunds
        for subscription in ("sub_a", "sub_b"):
            account_id = await paying(h, subscription)
            h.provider.failures["refund"] = RefundImpossible("Stripe refuses")
            assert (await erase(h, account_id)).ok
            del h.provider.failures["refund"]
            h.clock.advance(minutes=1)
        first, second = await book.listing()
        assert await book.sweep() == 2

        settled = await book.settle(first.id)
        h.clock.advance(days=1)
        again = await book.settle(first.id)

        assert settled is not None and again is not None
        assert settled.settled_at == again.settled_at  # the first time stays
        assert [r.id for r in await book.listing()] == [second.id]
        assert [r.id for r in await book.listing(settled=True)] == [first.id, second.id]
        assert await book.settle(UUID(int=0)) is None
        assert await book.sweep() == 1
        h.clock.advance(days=29)
        assert await book.sweep() == 1
        assert len(await book.listing(settled=True)) == 2  # 30 days are not over
        h.clock.advance(days=1, seconds=1)
        assert await book.sweep() == 1
        assert [r.id for r in await book.listing(settled=True)] == [second.id]


async def test_operators_list_and_settle_manual_refunds_over_the_api() -> None:
    """``can_manage_accounts`` on ``platform:main``, the gate of the operator area: an operator
    admin lists and settles, an account owner gets 403, nobody without a token gets in."""
    async with harness() as h:
        record = ManualRefund(
            id=UUID(int=7),
            key="pi_1:in_1",
            payment_ref="pi_1",
            invoice_ref="in_1",
            amount=600,
            currency="eur",
            reason="Stripe refuses",
            created_at=h.clock(),
        )
        async with h.store.transaction() as tx:
            assert await tx.add_manual_refund(record)
            assert not await tx.add_manual_refund(record)  # one per payment and invoice
        operator = await h.operator()
        owner = await h.owner()
        listing = f"{API}/operator/manual-refunds"
        settle = f"{listing}/{record.id}/settle"

        anonymous = await h.client.get(listing)
        refused = await h.client.get(listing, headers=owner.headers)
        refused_settle = await h.client.post(settle, headers=owner.headers)
        listed = await h.client.get(listing, headers=operator.headers)
        h.clock.advance(minutes=5)
        settled = await h.client.post(settle, headers=operator.headers)
        open_after = await h.client.get(listing, headers=operator.headers)
        with_settled = await h.client.get(
            listing, params={"settled": "true"}, headers=operator.headers
        )
        unknown = await h.client.post(f"{listing}/{UUID(int=1)}/settle", headers=operator.headers)

        assert anonymous.status_code == 401
        assert (refused.status_code, refused_settle.status_code) == (403, 403)
        assert listed.status_code == 200
        assert listed.json() == [
            {
                "id": str(record.id),
                "payment_ref": "pi_1",
                "invoice_ref": "in_1",
                "amount": 600,
                "currency": "eur",
                "reason": "Stripe refuses",
                "created_at": record.created_at.isoformat().replace("+00:00", "Z"),
                "settled_at": None,
            }
        ]
        assert settled.status_code == 200
        assert settled.json()["settled_at"] is not None
        assert open_after.json() == []
        assert [r["id"] for r in with_settled.json()] == [str(record.id)]
        assert unknown.status_code == 404


def test_the_gauge_reports_the_open_records() -> None:
    registry = CollectorRegistry()
    metrics = BillingMetrics(registry)
    metrics.manual_refunds_open(3)
    assert registry.get_sample_value("mowdeck_billing_manual_refunds_open") == 3
    BillingMetrics(None).manual_refunds_open(1)  # metrics off: a private registry


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
