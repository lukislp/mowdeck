# SPDX-FileCopyrightText: 2026 Lukas Körber
# SPDX-License-Identifier: AGPL-3.0-only

"""PostgreSQL: migrations up and down, schema drift, and the store contract.

The PostgreSQL tests run when ``MOWDECK_TEST_DATABASE_URL`` points at a server where the user
may create databases (CI: ``postgresql://postgres:test@127.0.0.1:5433/postgres``). Each test
creates its own database and drops it afterwards. The store contract also runs against the
memory store, so both implementations behave the same. Locally::

    docker run -d --name billing-test-pg -p 5433:5432 -e POSTGRES_PASSWORD=test \\
        postgres:17.11-alpine
    MOWDECK_TEST_DATABASE_URL=postgresql://postgres:test@127.0.0.1:5433/postgres \\
        uv run pytest services/billing -k db
"""

import asyncio
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from billing_support import T0
from mowdeck_contracts.v1 import AccountKind, PlanStatus, SubscriptionStatus
from mowdeck_service_kit.db import async_database_url
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from billing.adapters.db.migrate import alembic_config, upgrade_with_retry
from billing.adapters.db.store import SqlStore
from billing.adapters.db.tables import metadata
from billing.adapters.memory import MemoryStore
from billing.application.ports import Store
from billing.domain.ledger import FactKind, PaymentFact
from billing.domain.models import (
    BillingAccount,
    Customer,
    EventOutcome,
    Invoice,
    ManualRefund,
    Subscription,
    WebhookEvent,
)

ADMIN_URL = os.environ.get("MOWDECK_TEST_DATABASE_URL")
needs_db = pytest.mark.skipif(not ADMIN_URL, reason="needs MOWDECK_TEST_DATABASE_URL")


@asynccontextmanager
async def temporary_database() -> AsyncIterator[str]:
    """Create a fresh database and yield its URL; drop it afterwards."""
    assert ADMIN_URL
    name = f"billing_test_{uuid4().hex[:10]}"
    admin = create_async_engine(async_database_url(ADMIN_URL), isolation_level="AUTOCOMMIT")
    async with admin.connect() as conn:
        await conn.execute(text(f'CREATE DATABASE "{name}"'))
    base, _, _ = ADMIN_URL.rpartition("/")
    try:
        yield f"{base}/{name}"
    finally:
        async with admin.connect() as conn:
            await conn.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        await admin.dispose()


async def migrate(
    url: str, monkeypatch: pytest.MonkeyPatch, revision: str = "head", *, down: bool = False
) -> None:
    monkeypatch.setenv("MOWDECK_DATABASE_URL", url)
    if down:
        await asyncio.to_thread(command.downgrade, alembic_config(), revision)
    else:
        await asyncio.to_thread(command.upgrade, alembic_config(), revision)


async def table_names(engine: AsyncEngine) -> set[str]:
    async with engine.connect() as conn:
        rows = await conn.execute(
            text("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")
        )
        return {row[0] for row in rows}


@needs_db
async def test_billing_db_migrations_up_down_and_no_drift(monkeypatch: pytest.MonkeyPatch) -> None:
    async with temporary_database() as url:
        await migrate(url, monkeypatch)
        engine = create_async_engine(async_database_url(url))
        try:
            assert set(metadata.tables) <= await table_names(engine)

            def diff(sync_conn: Any) -> list[Any]:
                context = MigrationContext.configure(sync_conn)
                return list(compare_metadata(context, metadata))

            async with engine.connect() as conn:
                assert await conn.run_sync(diff) == []
            await migrate(url, monkeypatch, "0001", down=True)
            assert not set(metadata.tables) & await table_names(engine)
            await migrate(url, monkeypatch)
            assert set(metadata.tables) <= await table_names(engine)
        finally:
            await engine.dispose()


async def store_contract(store: Store) -> None:
    """Behaviour every store implementation must have."""
    account_id = uuid4()
    async with store.transaction() as tx:
        assert await tx.save_account_if_absent(
            BillingAccount(account_id, "Garden", AccountKind.BUSINESS, T0)
        )
        assert not await tx.save_account_if_absent(BillingAccount(account_id, "Again"))
        assert await tx.get_account(uuid4()) is None
    async with store.transaction() as tx:
        found = await tx.get_account(account_id)
        assert found is not None
        assert (found.name, found.kind) == ("Garden", AccountKind.BUSINESS)

        await tx.save_customer(Customer(account_id, "stripe", "cus_1", T0))
        await tx.save_customer(Customer(account_id, "stripe", "cus_2", T0))  # ignored
        await tx.save_customer(Customer(uuid4(), "stripe", "cus_1", T0))  # id taken: ignored
        customer = await tx.get_customer(account_id, "stripe")
        assert customer is not None
        assert customer.provider_customer_id == "cus_1"
        by_id = await tx.find_customer("stripe", "cus_1")
        assert by_id is not None
        assert by_id.account_id == account_id
        assert await tx.find_customer("stripe", "cus_2") is None
        assert await tx.get_customer(account_id, "fake") is None
        locked = await tx.lock_customer("stripe", "cus_1")
        assert locked is not None and locked.account_id == account_id
        assert await tx.lock_customer("stripe", "cus_2") is None
        assert await tx.lock_customer("fake", "cus_1") is None
        await tx.save_customer(Customer(account_id, "fake", "cus_f", T0))
        assert [c.provider for c in await tx.customers_of(account_id)] == ["fake", "stripe"]
        assert await tx.customers_of(uuid4()) == []

    first = Subscription(
        id=uuid4(),
        account_id=account_id,
        provider="stripe",
        provider_subscription_id="sub_1",
        plan_key="home",
        status=SubscriptionStatus.ACTIVE,
        created_at=T0,
        updated_at=T0,
        current_period_end=T0 + timedelta(days=30),
        plan_status=PlanStatus.ACTIVE,
        last_event_at=T0,
    )
    async with store.transaction() as tx:
        await tx.save_subscription(first)
        second = Subscription(
            id=uuid4(),
            account_id=account_id,
            provider="stripe",
            provider_subscription_id="sub_2",
            plan_key=None,
            status=SubscriptionStatus.INCOMPLETE,
            created_at=T0 + timedelta(hours=1),
            updated_at=T0,
        )
        await tx.save_subscription(second)
        changed = Subscription(
            id=uuid4(),  # ignored on update
            account_id=account_id,
            provider="stripe",
            provider_subscription_id="sub_1",
            plan_key="home",
            status=SubscriptionStatus.PAST_DUE,
            created_at=T0 + timedelta(days=9),  # ignored on update
            updated_at=T0 + timedelta(days=1),
            plan_status=PlanStatus.GRACE,
            grace_until=T0 + timedelta(days=14),
            cancel_at_period_end=True,
        )
        await tx.save_subscription(changed)
    async with store.transaction() as tx:
        stored = await tx.get_subscription("stripe", "sub_1")
        assert stored is not None
        assert (stored.id, stored.created_at, stored.status, stored.plan_status) == (
            first.id,
            T0,
            SubscriptionStatus.PAST_DUE,
            PlanStatus.GRACE,
        )
        assert stored.cancel_at_period_end
        assert stored.grace_until == T0 + timedelta(days=14)
        listed = await tx.subscriptions_of(account_id)
        assert [s.provider_subscription_id for s in listed] == ["sub_2", "sub_1"]
        assert await tx.get_subscription("stripe", "sub_9") is None
        assert await tx.subscriptions_of(uuid4()) == []

    async with store.transaction() as tx:
        for number in range(3):
            await tx.save_invoice(
                Invoice(
                    id=uuid4(),
                    account_id=account_id,
                    provider="stripe",
                    provider_invoice_id=f"in_{number}",
                    status="open",
                    currency="eur",
                    amount_due=500,
                    amount_paid=0,
                    issued_at=T0 + timedelta(days=number),
                    updated_at=T0,
                )
            )
        await tx.save_invoice(
            Invoice(
                id=uuid4(),
                account_id=uuid4(),  # the account of an invoice never changes
                provider="stripe",
                provider_invoice_id="in_0",
                status="paid",
                currency="eur",
                amount_due=500,
                amount_paid=500,
                issued_at=T0,
                updated_at=T0 + timedelta(days=5),
                number="MD-1",
                hosted_url="https://invoice",
                pdf_url="https://pdf",
            )
        )
    async with store.transaction() as tx:
        invoices = await tx.invoices_of(account_id, 2)
        assert [i.provider_invoice_id for i in invoices] == ["in_2", "in_1"]
        everything = await tx.invoices_of(account_id, 10)
        paid = next(i for i in everything if i.provider_invoice_id == "in_0")
        assert (paid.status, paid.amount_paid, paid.number, paid.pdf_url) == (
            "paid",
            500,
            "MD-1",
            "https://pdf",
        )

    async with store.transaction() as tx:
        await tx.record_event(WebhookEvent("stripe", "evt_1", "x", T0))
        await tx.record_event(WebhookEvent("stripe", "evt_1", "other", T0 + timedelta(hours=1)))
        assert await tx.claim_event("stripe", "evt_1", T0, 60)
        assert not await tx.claim_event("stripe", "evt_1", T0 + timedelta(seconds=30), 60)
        assert not await tx.claim_event("stripe", "evt_missing", T0, 60)
    async with store.transaction() as tx:
        # The lease expired: another delivery may take it over.
        assert await tx.claim_event("stripe", "evt_1", T0 + timedelta(seconds=61), 60)
        await tx.finish_event("stripe", "evt_1", EventOutcome.FAILED, None)
        assert await tx.claim_event("stripe", "evt_1", T0 + timedelta(seconds=62), 60)
        await tx.attribute_event("stripe", "evt_1", account_id)
        await tx.finish_event("stripe", "evt_1", EventOutcome.APPLIED, T0 + timedelta(minutes=2))
        assert not await tx.claim_event("stripe", "evt_1", T0 + timedelta(days=1), 60)
        await tx.finish_event("stripe", "evt_missing", EventOutcome.APPLIED, T0)
        await tx.attribute_event("stripe", "evt_missing", account_id)
    async with store.transaction() as tx:
        event = await tx.get_event("stripe", "evt_1")
        assert event is not None
        assert (event.type, event.attempts, event.outcome, event.account_id) == (
            "x",
            3,
            EventOutcome.APPLIED,
            account_id,
        )
        assert event.processed_at == T0 + timedelta(minutes=2)
        assert await tx.get_event("stripe", "evt_missing") is None

    # Everything above belongs to one account; another account's rows survive its purge.
    other = uuid4()
    async with store.transaction() as tx:
        await tx.save_account_if_absent(BillingAccount(other, "Other"))
        await tx.save_customer(Customer(other, "stripe", "cus_other", T0))
        await tx.record_event(WebhookEvent("stripe", "evt_unknown", "customer.deleted", T0))
        before = await tx.account_rows(account_id)
        assert before == {
            "accounts": 1,
            "customers": 2,
            "subscriptions": 2,
            "invoices": 3,
            "webhook_events": 1,
        }
    async with store.transaction() as tx:
        assert await tx.purge_account(account_id) == sum(before.values())
        assert await tx.purge_account(account_id) == 0
        assert await tx.account_rows(account_id) == {}
        assert await tx.account_rows(other) == {"accounts": 1, "customers": 1}
        assert await tx.find_customer("stripe", "cus_1") is None
        assert await tx.get_event("stripe", "evt_1") is None
        # An event that belongs to no account is nobody's and stays.
        assert await tx.get_event("stripe", "evt_unknown") is not None


async def manual_refund_contract(store: Store) -> None:
    """The record of refunds to pay by hand: one per key, listed oldest first, settled once,
    deleted after the retention only when settled."""
    at = datetime(2026, 11, 12, tzinfo=UTC)

    def record(key: str, minutes: int) -> ManualRefund:
        return ManualRefund(
            id=uuid4(),
            key=key,
            payment_ref=key.split(":")[0],
            invoice_ref=key.split(":")[1] or None,
            amount=600,
            currency="eur",
            reason="refused",
            created_at=at + timedelta(minutes=minutes),
        )

    first, second, twin = record("pi_1:in_1", 0), record("pi_2:", 1), record("pi_1:in_1", 2)
    async with store.transaction() as tx:
        assert await tx.add_manual_refund(first)
        assert await tx.add_manual_refund(second)
        assert not await tx.add_manual_refund(twin)
        assert [r.id for r in await tx.manual_refunds(settled=False)] == [first.id, second.id]
        assert await tx.open_manual_refunds() == 2
        settled = await tx.settle_manual_refund(first.id, at + timedelta(days=1))
        assert settled is not None and settled.settled_at == at + timedelta(days=1)
        again = await tx.settle_manual_refund(first.id, at + timedelta(days=2))
        assert again is not None and again.settled_at == at + timedelta(days=1)
        assert await tx.settle_manual_refund(uuid4(), at) is None
        assert [r.id for r in await tx.manual_refunds(settled=False)] == [second.id]
        assert await tx.open_manual_refunds() == 1
        assert second.invoice_ref is None
        assert await tx.delete_settled_manual_refunds(at + timedelta(days=1)) == 0
        assert await tx.delete_settled_manual_refunds(at + timedelta(days=40)) == 1
        assert [r.id for r in await tx.manual_refunds(settled=True)] == [second.id]


async def ledger_contract(store: Store) -> None:
    """The payments ledger (ADR 0083): one fact per kind and keyed hash, updated only by a newer
    event, read by occurrence (open disputes always), hashes cleared after the horizon; and the
    live tallies of subscriptions and open invoices."""
    at = datetime(2026, 11, 12, tzinfo=UTC)

    def fact(kind: FactKind, ref: str | None, occurred: datetime, **values: Any) -> PaymentFact:
        return PaymentFact(
            id=uuid4(),
            kind=kind,
            ref=ref,
            provider="stripe",
            occurred_at=occurred,
            event_at=values.pop("event_at", occurred),
            recorded_at=values.pop("recorded_at", at),
            updated_at=values.pop("updated_at", at),
            **values,
        )

    refund = fact(FactKind.REFUND, "r1", at, currency="eur", amount=450, status="pending")
    async with store.transaction() as tx:
        assert await tx.first_fact_at() is None
        assert await tx.record_fact(refund)
        # The same event again, and an older one: nothing changes.
        again = fact(FactKind.REFUND, "r1", at, status="pending", currency="eur", amount=450)
        assert not await tx.record_fact(again)
        stale = fact(FactKind.REFUND, "r1", at, status="failed", event_at=at - timedelta(1))
        assert not await tx.record_fact(stale)
        newer = fact(
            FactKind.REFUND,
            "r1",
            at + timedelta(days=2),  # written once: stays
            currency="usd",  # written once: stays
            amount=450,
            status="succeeded",
            event_at=at + timedelta(hours=1),
            updated_at=at + timedelta(hours=1),
        )
        assert await tx.record_fact(newer)
        # The same hash under another kind is another fact.
        old = at - timedelta(days=90)
        assert await tx.record_fact(fact(FactKind.DISPUTE, "r1", old, status="needs_response"))
        closed = at - timedelta(days=60)
        assert await tx.record_fact(
            fact(FactKind.DISPUTE, "d2", old, status="won", closed_at=closed)
        )
        assert await tx.record_fact(
            fact(
                FactKind.INVOICE_PAID, "i1", at - timedelta(days=40), recorded_at=at - timedelta(1)
            )
        )
        # Facts whose hash was cleared never collide.
        assert await tx.record_fact(fact(FactKind.INVOICE_PAID, None, at))
        assert await tx.record_fact(fact(FactKind.INVOICE_PAID, None, at))
    async with store.transaction() as tx:
        listed = await tx.payment_facts(at - timedelta(days=30))
        kinds = sorted((f.kind.value, f.status or "") for f in listed)
        # The old open dispute is listed, the old closed one and the old invoice are not.
        assert kinds == [
            ("dispute", "needs_response"),
            ("invoice_paid", ""),
            ("invoice_paid", ""),
            ("refund", "succeeded"),
        ]
        stored = next(f for f in listed if f.kind is FactKind.REFUND)
        assert (stored.id, stored.occurred_at, stored.currency, stored.amount) == (
            refund.id,
            at,
            "eur",
            450,
        )
        assert stored.event_at == at + timedelta(hours=1)
        assert len(await tx.payment_facts(at - timedelta(days=100))) == 6
        assert await tx.first_fact_at() == at - timedelta(1)
        assert await tx.forget_fact_refs(at + timedelta(minutes=1)) == 3
        assert await tx.forget_fact_refs(at + timedelta(minutes=1)) == 0
    async with store.transaction() as tx:
        listed = await tx.payment_facts(at - timedelta(days=100))
        assert sorted(f.ref or "-" for f in listed) == ["-", "-", "-", "-", "-", "r1"]

    account_id = uuid4()
    async with store.transaction() as tx:
        for number, (plan, status) in enumerate(
            [("home", "active"), ("home", "active"), ("home", "trialing"), (None, "past_due")]
        ):
            await tx.save_subscription(
                Subscription(
                    id=uuid4(),
                    account_id=account_id,
                    provider="stripe",
                    provider_subscription_id=f"sub_{number}",
                    plan_key=plan,
                    status=SubscriptionStatus(status),
                    created_at=T0,
                    updated_at=T0,
                )
            )
        for number, (status, due, paid) in enumerate([("open", 990, 0), ("open", 990, 490),
                                                      ("paid", 990, 990)]):  # fmt: skip
            await tx.save_invoice(
                Invoice(
                    id=uuid4(),
                    account_id=account_id,
                    provider="stripe",
                    provider_invoice_id=f"in_{number}",
                    status=status,
                    currency="eur",
                    amount_due=due,
                    amount_paid=paid,
                    issued_at=T0 + timedelta(days=number),
                    updated_at=T0,
                )
            )
    async with store.transaction() as tx:
        tally = {(t.plan_key, t.status.value, t.count) for t in await tx.subscription_tally()}
        assert tally == {("home", "active", 2), ("home", "trialing", 1), (None, "past_due", 1)}
        assert [(i.currency, i.amount_due, i.due_at) for i in await tx.open_invoices()] == [
            ("eur", 990, T0),
            ("eur", 500, T0 + timedelta(days=1)),
        ]


@asynccontextmanager
async def store_of(kind: str, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[Store]:
    """The memory store, or the SQL store on a fresh migrated database."""
    if kind == "memory":
        yield MemoryStore()
        return
    async with temporary_database() as url:
        await migrate(url, monkeypatch)
        engine = create_async_engine(async_database_url(url))
        try:
            yield SqlStore(engine)
        finally:
            await engine.dispose()


STORES = [pytest.param("memory"), pytest.param("postgres", marks=needs_db)]


async def test_billing_memory_store_contract() -> None:
    await store_contract(MemoryStore())
    await manual_refund_contract(MemoryStore())
    await ledger_contract(MemoryStore())


async def test_billing_memory_store_rolls_back_on_error() -> None:
    store = MemoryStore()
    with pytest.raises(RuntimeError):
        async with store.transaction() as tx:
            await tx.save_account_if_absent(BillingAccount(uuid4()))
            raise RuntimeError
    assert store.data.accounts == {}


@needs_db
async def test_billing_db_store_contract(monkeypatch: pytest.MonkeyPatch) -> None:
    async with temporary_database() as url:
        await migrate(url, monkeypatch)
        engine = create_async_engine(async_database_url(url))
        try:
            await store_contract(SqlStore(engine))
            await manual_refund_contract(SqlStore(engine))
            await ledger_contract(SqlStore(engine))
        finally:
            await engine.dispose()


@needs_db
async def test_billing_db_webhook_path_end_to_end(monkeypatch: pytest.MonkeyPatch) -> None:
    from billing_support import harness
    from test_billing_webhooks import _sub

    async with temporary_database() as url:
        await migrate(url, monkeypatch)
        engine = create_async_engine(async_database_url(url))
        try:
            async with harness(store=SqlStore(engine)) as h:  # type: ignore[arg-type]
                owner = await h.owner()
                await h.customer(owner.account_id)
                event = _sub(h, owner.account_id, "past_due")
                assert (await h.webhook(event)).json()["outcome"] == "applied"
                assert (await h.webhook(event)).json()["duplicate"] is True
                summary = await h.client.get("/api/v1/billing/subscription", headers=owner.headers)
                assert summary.json()["subscription"]["plan_status"] == "grace"
        finally:
            await engine.dispose()


def test_billing_migrate_retries_refused_connections() -> None:
    calls: list[int] = []

    def upgrade() -> None:
        calls.append(1)
        if len(calls) < 3:
            raise OSError("refused")

    upgrade_with_retry(upgrade, attempts=5, delay_seconds=0, sleep=lambda _: None)
    assert len(calls) == 3
    with pytest.raises(OSError, match="refused"):
        upgrade_with_retry(
            lambda: (_ for _ in ()).throw(OSError("refused")),
            attempts=2,
            delay_seconds=0,
            sleep=lambda _: None,
        )
