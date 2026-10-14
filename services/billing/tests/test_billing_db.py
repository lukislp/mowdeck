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
from datetime import timedelta
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
from billing.domain.models import (
    BillingAccount,
    Customer,
    EventOutcome,
    Invoice,
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
        await tx.finish_event("stripe", "evt_1", EventOutcome.FAILED, None, None)
        assert await tx.claim_event("stripe", "evt_1", T0 + timedelta(seconds=62), 60)
        await tx.finish_event(
            "stripe", "evt_1", EventOutcome.APPLIED, T0 + timedelta(minutes=2), account_id
        )
        assert not await tx.claim_event("stripe", "evt_1", T0 + timedelta(days=1), 60)
        await tx.finish_event("stripe", "evt_missing", EventOutcome.APPLIED, T0, None)
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


async def test_billing_memory_store_contract() -> None:
    await store_contract(MemoryStore())


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
