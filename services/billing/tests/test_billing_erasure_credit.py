# SPDX-FileCopyrightText: 2026 Lukas Körber
# SPDX-License-Identifier: AGPL-3.0-only

"""The credit balance of an erased account is paid back (ADR 0082, section 5), through the real
app: Identity's request over the in-memory bus, the fake payment provider, the memory store and,
with ``MOWDECK_TEST_DATABASE_URL``, PostgreSQL.

The order at the provider is: period refunds, cancel, credit refunds, zero the balance, delete
the customer; then the rows. A refund that fails stops the erasure before the balance is
zeroed, a retry never refunds twice, a skip and a leftover are recorded in the transaction that
deletes the rows, and a retry after the balance was zeroed zeroes nothing.

The account pays 9.00 EUR a month (``pi_sub_1``, the month's invoice: 6.00 of it is refunded
for the unused period) and paid 9.00 a month before (``pi_oct``) and before that (``pi_sep``).
It holds a credit of 15.00 EUR: 3.00 from ``pi_sub_1``, 9.00 from ``pi_oct``, 3.00 from
``pi_sep``.
"""

from datetime import timedelta
from typing import Any
from uuid import UUID

import pytest
import structlog
from billing_support import Harness, harness
from mowdeck_contracts.v1 import AccountEraseReply
from test_billing_db import STORES, store_of
from test_billing_erasure import erase, rows_of
from test_billing_erasure_refunds import paying, records

from billing.adapters.db.store import SqlTx
from billing.adapters.memory import MemoryTx
from billing.adapters.providers.fake import fake_customer_id
from billing.adapters.providers.fixtures import refund_event
from billing.domain.credit import credit_metadata
from billing.domain.errors import ProviderUnavailable, RefundImpossible
from billing.domain.models import CreditBalance, ProviderRefund, RecentPayment, RefundStatus

CREDIT = 1500


async def with_credit(h: Harness, credit: int = CREDIT, currency: str = "eur") -> tuple[UUID, str]:
    """A paying account with a credit balance and two older payments."""
    account_id = await paying(h)
    customer = fake_customer_id(account_id)
    h.provider.balances[customer] = CreditBalance(credit, currency)
    now = h.clock()
    h.provider.payments[customer] = [
        RecentPayment("pi_sep", 900, "eur", now - timedelta(days=70)),
        RecentPayment("pi_sub_1", 900, "eur", now - timedelta(days=10)),
        RecentPayment("pi_oct", 900, "eur", now - timedelta(days=40)),
    ]
    return account_id, customer


def amounts(h: Harness, payment: str) -> list[tuple[str, RefundStatus, int]]:
    return [(r.metadata.get("reason", ""), r.status, r.amount) for r in h.provider.refunds[payment]]


def refusing(h: Harness, payment: str) -> None:
    """The provider refuses the credit refund on ``payment`` for good (a disputed payment)."""
    original = h.provider.refund

    async def refund(
        payment_ref: str,
        amount: int,
        currency: str,
        idempotency_key: str,
        metadata: dict[str, str],
    ) -> ProviderRefund:
        if payment_ref == payment and metadata.get("reason") == "account_erasure_credit_balance":
            h.provider.calls.append(("refund", payment_ref))
            raise RefundImpossible("Stripe refuses (charge_disputed)")
        return await original(payment_ref, amount, currency, idempotency_key, metadata)

    h.provider.refund = refund  # type: ignore[method-assign]


@pytest.mark.parametrize("kind", STORES)
async def test_the_credit_is_paid_back_after_the_cancel_and_before_the_customer_goes(
    kind: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with store_of(kind, monkeypatch) as store, harness(store=store) as h:  # type: ignore[arg-type]
        account_id, customer = await with_credit(h)
        before = await rows_of(store, account_id)

        reply = await erase(h, account_id)

        assert reply == AccountEraseReply(ok=True, deleted=sum(before.values()))
        assert h.provider.calls == [
            ("refund", "pi_sub_1"),  # the unused period: 6.00
            ("cancel_subscription", "sub_1"),
            ("refund", "pi_sub_1"),  # the credit, newest payment first: 3.00 left of it
            ("refund", "pi_oct"),
            ("refund", "pi_sep"),
            ("clear_credit_balance", customer),
            ("delete_customer", customer),
        ]
        credit = "account_erasure_credit_balance"
        assert amounts(h, "pi_sub_1") == [
            ("account_erasure", RefundStatus.SUCCEEDED, 600),
            (credit, RefundStatus.SUCCEEDED, 300),
        ]
        assert amounts(h, "pi_oct") == [(credit, RefundStatus.SUCCEEDED, 900)]
        assert amounts(h, "pi_sep") == [(credit, RefundStatus.SUCCEEDED, 300)]
        assert h.provider.refunds["pi_oct"][0].metadata == credit_metadata(account_id, "pi_oct")
        assert f"mowdeck-erasure-credit-{account_id}-pi_oct" in h.provider.idempotency_keys
        assert h.provider.balance_keys == {
            f"mowdeck-erasure-credit-clear-{account_id}-{customer}-1500eur": CREDIT
        }
        assert h.provider.balances[customer] == CreditBalance(0, "eur", CREDIT)
        assert await records(store) == []
        assert await rows_of(store, account_id) == {}


async def test_ending_the_subscription_adds_no_credit() -> None:
    """Without a credit balance the erasure is what it was: no credit refund, no zeroing."""
    async with harness() as h:
        account_id = await paying(h)
        customer = fake_customer_id(account_id)

        assert (await erase(h, account_id)).ok

        assert [c[0] for c in h.provider.calls] == [
            "refund",
            "cancel_subscription",
            "delete_customer",
        ]
        assert h.provider.balance_keys == {}
        assert customer in h.provider.deleted


async def test_a_customer_who_owes_money_is_never_charged() -> None:
    async with harness() as h:
        account_id, customer = await with_credit(h, credit=-2000)

        assert (await erase(h, account_id)).ok

        assert [c[0] for c in h.provider.calls] == [
            "refund",
            "cancel_subscription",
            "delete_customer",
        ]
        assert h.provider.balances[customer] == CreditBalance(-2000, "eur")
        assert await records(h.store) == []


@pytest.mark.parametrize("kind", STORES)
async def test_a_failed_credit_refund_fails_the_erasure_and_the_retry_refunds_once(
    kind: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``pi_oct`` fails: the balance is not zeroed, the customer and the rows stay. The retry
    finds the period refund and the credit refund on ``pi_sub_1``, refunds ``pi_oct`` under a
    new key and goes on."""
    async with store_of(kind, monkeypatch) as store, harness(store=store) as h:  # type: ignore[arg-type]
        account_id, customer = await with_credit(h)
        before = await rows_of(store, account_id)
        statuses = iter(
            [RefundStatus.SUCCEEDED, RefundStatus.SUCCEEDED, RefundStatus.FAILED]
            + [RefundStatus.PENDING] * 2
        )
        original = h.provider.refund

        async def refund_with(*args: Any, **kwargs: Any) -> ProviderRefund:
            h.provider.refund_status = next(statuses)
            return await original(*args, **kwargs)

        h.provider.refund = refund_with  # type: ignore[method-assign]

        failed = await erase(h, account_id)
        left = await rows_of(store, account_id)
        balance = h.provider.balances[customer]
        retried = await erase(h, account_id)

        assert failed == AccountEraseReply(
            ok=False, deleted=0, error="RefundFailed: see the log of billing"
        )
        assert left == before
        assert balance == CreditBalance(CREDIT, "eur")
        assert retried == AccountEraseReply(ok=True, deleted=sum(before.values()))
        assert h.provider.calls == [
            ("refund", "pi_sub_1"),
            ("cancel_subscription", "sub_1"),
            ("refund", "pi_sub_1"),
            ("refund", "pi_oct"),
            # the retry
            ("cancel_subscription", "sub_1"),
            ("refund", "pi_oct"),
            ("refund", "pi_sep"),
            ("clear_credit_balance", customer),
            ("delete_customer", customer),
        ]
        credit = "account_erasure_credit_balance"
        assert [a for a in amounts(h, "pi_sub_1") if a[0] == credit] == [
            (credit, RefundStatus.SUCCEEDED, 300)
        ]
        assert amounts(h, "pi_oct") == [
            (credit, RefundStatus.FAILED, 900),
            (credit, RefundStatus.PENDING, 900),
        ]
        assert amounts(h, "pi_sep") == [(credit, RefundStatus.PENDING, 300)]
        key = f"mowdeck-erasure-credit-{account_id}-pi_oct"
        assert {key, f"{key}-1"} <= set(h.provider.idempotency_keys)
        assert h.provider.balances[customer] == CreditBalance(0, "eur", CREDIT)


async def test_an_already_zero_balance_is_not_zeroed_again_on_retry() -> None:
    """The balance was zeroed, then deleting the customer failed. The retry reads 0 (and what
    the erasure cleared), finds every refund done and makes no balance transaction."""
    async with harness() as h:
        account_id, customer = await with_credit(h)
        h.provider.failures["delete_customer"] = ProviderUnavailable("outage")
        assert not (await erase(h, account_id)).ok
        del h.provider.failures["delete_customer"]
        refunds_before = {k: list(v) for k, v in h.provider.refunds.items()}
        h.clock.advance(days=2)

        assert (await erase(h, account_id)).ok

        assert [c for c in h.provider.calls if c[0] == "clear_credit_balance"] == [
            ("clear_credit_balance", customer)
        ]
        assert h.provider.calls[-2:] == [
            ("cancel_subscription", "sub_1"),
            ("delete_customer", customer),
        ]
        assert h.provider.refunds == refunds_before
        assert await records(h.store) == []


@pytest.mark.parametrize("kind", STORES)
async def test_a_refused_credit_refund_and_the_leftover_are_recorded_with_the_rows(
    kind: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``pi_oct`` is disputed: its 9.00 is skipped and recorded. 20.00 of credit: 3.00 from
    ``pi_sub_1``, 9.00 recorded for ``pi_oct``, 8.00 from ``pi_sep``, nothing left over."""
    async with store_of(kind, monkeypatch) as store, harness(store=store) as h:  # type: ignore[arg-type]
        account_id, customer = await with_credit(h, credit=2000)
        refusing(h, "pi_oct")

        with structlog.testing.capture_logs() as logs:
            reply = await erase(h, account_id)

        assert reply.ok
        assert await rows_of(store, account_id) == {}
        [record] = await records(store)
        assert (record.key, record.payment_ref, record.invoice_ref) == ("pi_oct:", "pi_oct", None)
        assert (record.amount, record.currency) == (900, "eur")
        assert record.reason == "credit balance: Stripe refuses (charge_disputed)"
        assert str(account_id) not in repr(record)
        assert [r.amount for r in h.provider.refunds["pi_sep"]] == [800]
        assert h.provider.balances[customer] == CreditBalance(0, "eur", 2000)
        [warning] = [e for e in logs if e["log_level"] == "warning"]
        assert warning["event"] == (
            "refund of an erased account skipped: recorded to pay it out by hand"
        )
        assert (warning["customer_id"], warning["amount"]) == (customer, 900)


async def test_the_part_no_payment_can_take_is_recorded_once() -> None:
    """24.00 of credit: 3.00 + 9.00 + 9.00 refunded, 3.00 recorded against the newest payment."""
    async with harness() as h:
        account_id, customer = await with_credit(h, credit=2400)

        with structlog.testing.capture_logs() as logs:
            assert (await erase(h, account_id)).ok

        [record] = await records(h.store)
        assert (record.key, record.payment_ref, record.amount) == (
            f"{customer}:credit-balance",
            "pi_sub_1",
            300,
        )
        assert record.reason == "credit balance: the recent payments are refunded in full"
        assert h.provider.balances[customer] == CreditBalance(0, "eur", 2400)
        [warning] = [e for e in logs if e["log_level"] == "warning"]
        assert warning["event"].startswith("credit balance of an erased account left over")


async def test_a_balance_in_another_currency_is_recorded_in_full() -> None:
    async with harness() as h:
        account_id, _customer = await with_credit(h, credit=700, currency="usd")

        assert (await erase(h, account_id)).ok

        assert [c[0] for c in h.provider.calls] == [
            "refund",
            "cancel_subscription",
            "clear_credit_balance",
            "delete_customer",
        ]
        [record] = await records(h.store)
        assert (record.amount, record.currency) == (700, "usd")
        assert record.reason == "credit balance: the credit is in usd, the payments in eur"


@pytest.mark.parametrize("kind", STORES)
async def test_records_survive_a_failed_row_deletion_after_the_customer_was_deleted(
    kind: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A skip (``pi_oct``, 9.00) and a leftover (3.00). Stripe confirms the customer deletion,
    then the transaction that deletes the rows fails. The records were written before the
    customer went: they exist, the rows stay. The retry finds no customer (and so no balance),
    deletes the rows, and each record exists exactly once."""

    async def purge_fails(self: object, account_id: UUID) -> int:
        del self, account_id
        raise RuntimeError("database gone")

    async with store_of(kind, monkeypatch) as store, harness(store=store) as h:  # type: ignore[arg-type]
        account_id, customer = await with_credit(h, credit=2400)
        before = await rows_of(store, account_id)
        refusing(h, "pi_oct")
        with monkeypatch.context() as patched:
            patched.setattr(MemoryTx, "purge_account", purge_fails)
            patched.setattr(SqlTx, "purge_account", purge_fails)
            failed = await erase(h, account_id)
        written = await records(store)
        left = await rows_of(store, account_id)
        gone = customer in h.provider.deleted

        retried = await erase(h, account_id)

        assert not failed.ok
        assert gone
        assert left == before
        expected = [(f"{customer}:credit-balance", "pi_sub_1", 300), ("pi_oct:", "pi_oct", 900)]
        assert sorted((r.key, r.payment_ref, r.amount) for r in written) == expected
        assert retried.ok
        after = await records(store)
        assert sorted((r.key, r.payment_ref, r.amount) for r in after) == expected
        assert {r.id for r in after} == {r.id for r in written}
        assert await rows_of(store, account_id) == {}


async def test_a_retry_after_the_balance_was_zeroed_keeps_the_recorded_skip() -> None:
    """Deleting the customer fails after the balance was zeroed; the records are written by
    then. The retry still knows the credit (``cleared``), finds the refunds done, keeps the
    recorded skip of ``pi_oct`` without asking Stripe again (Stripe would take it now) and
    inserts nothing twice."""
    async with harness() as h:
        account_id, customer = await with_credit(h, credit=2400)
        refusing(h, "pi_oct")
        h.provider.failures["delete_customer"] = ProviderUnavailable("outage")
        failed = await erase(h, account_id)
        written = await records(h.store)
        zeroed = h.provider.balances[customer]
        del h.provider.failures["delete_customer"]
        h.provider.refund = type(h.provider).refund.__get__(h.provider)  # type: ignore[method-assign]

        retried = await erase(h, account_id)

        assert not failed.ok
        assert zeroed == CreditBalance(0, "eur", 2400)
        expected = [(f"{customer}:credit-balance", "pi_sub_1", 300), ("pi_oct:", "pi_oct", 900)]
        assert sorted((r.key, r.payment_ref, r.amount) for r in written) == expected
        assert retried.ok
        assert sorted((r.key, r.payment_ref, r.amount) for r in await records(h.store)) == expected
        assert h.provider.calls.count(("refund", "pi_oct")) == 1
        assert "pi_oct" not in h.provider.refunds
        assert [c[0] for c in h.provider.calls].count("clear_credit_balance") == 1
        assert [r.amount for r in h.provider.refunds["pi_sep"]] == [900]


async def test_a_balance_that_cannot_be_read_fails_the_erasure() -> None:
    async with harness() as h:
        account_id, customer = await with_credit(h)
        h.provider.failures["recent_payments"] = ProviderUnavailable("rate limited")

        failed = await erase(h, account_id)

        assert failed.error == "ProviderUnavailable: see the log of billing"
        assert customer in h.provider.customers
        assert h.provider.balances[customer] == CreditBalance(CREDIT, "eur")


async def test_a_credit_refund_that_fails_after_the_erasure_is_recorded_for_the_operator() -> None:
    async with harness() as h:
        account_id, _customer = await with_credit(h)
        assert (await erase(h, account_id)).ok
        [made] = h.provider.refunds["pi_oct"]

        with structlog.testing.capture_logs() as logs:
            response = await h.webhook(
                refund_event(
                    "refund.failed",
                    refund_id=made.id,
                    payment_intent="pi_oct",
                    amount=900,
                    status="failed",
                    metadata=made.metadata,
                    failure_reason="expired_or_canceled_card",
                )
            )

        assert response.json()["outcome"] == "unknown_account"
        [record] = await records(h.store)
        assert (record.key, record.payment_ref, record.invoice_ref, record.amount) == (
            "pi_oct:",
            "pi_oct",
            None,
            900,
        )
        assert (
            record.reason == f"credit balance: refund {made.id} failed (expired_or_canceled_card)"
        )
        [warning] = [e for e in logs if e["log_level"] == "warning"]
        assert warning["event"] == "a refund of an account erasure failed: pay it out by hand"
