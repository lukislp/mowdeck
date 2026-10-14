"""PostgreSQL store on SQLAlchemy Core (async, asyncpg). Upserts use ``ON CONFLICT``.

The webhook ledger relies on the primary key ``(provider, event_id)``: two deliveries of one
event insert concurrently, the second waits for the first's transaction and then finds the
event claimed (``claim_event`` only updates an unclaimed or expired row).
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID

import sqlalchemy as sa
from mowdeck_contracts.v1 import AccountKind, PlanStatus, SubscriptionStatus
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from billing.adapters.db import tables
from billing.domain.models import (
    BillingAccount,
    Customer,
    EventOutcome,
    Invoice,
    Subscription,
    WebhookEvent,
)


def _subscription(row: Any) -> Subscription:
    values = dict(row._mapping)
    values["status"] = SubscriptionStatus(values["status"])
    values["plan_status"] = PlanStatus(values["plan_status"]) if values["plan_status"] else None
    return Subscription(**values)


def _subscription_values(s: Subscription) -> dict[str, Any]:
    return {
        "id": s.id,
        "account_id": s.account_id,
        "provider": s.provider,
        "provider_subscription_id": s.provider_subscription_id,
        "plan_key": s.plan_key,
        "status": s.status.value,
        "current_period_end": s.current_period_end,
        "trial_end": s.trial_end,
        "cancel_at_period_end": s.cancel_at_period_end,
        "plan_status": s.plan_status.value if s.plan_status else None,
        "grace_until": s.grace_until,
        "last_event_at": s.last_event_at,
        "created_at": s.created_at,
        "updated_at": s.updated_at,
    }


def _event(row: Any) -> WebhookEvent:
    values = dict(row._mapping)
    values["outcome"] = EventOutcome(values["outcome"]) if values["outcome"] else None
    return WebhookEvent(**values)


class SqlTx:
    """``Tx`` on one connection."""

    def __init__(self, conn: AsyncConnection) -> None:
        self._conn = conn

    async def get_account(self, account_id: UUID) -> BillingAccount | None:
        a = tables.accounts
        row = (await self._conn.execute(sa.select(a).where(a.c.account_id == account_id))).first()
        if row is None:
            return None
        values = dict(row._mapping)
        values["kind"] = AccountKind(values["kind"]) if values["kind"] else None
        return BillingAccount(**values)

    async def save_account_if_absent(self, account: BillingAccount) -> bool:
        statement = (
            pg_insert(tables.accounts)
            .values(
                account_id=account.account_id,
                name=account.name,
                kind=account.kind.value if account.kind else None,
                created_at=account.created_at,
            )
            .on_conflict_do_nothing(index_elements=["account_id"])
        )
        return bool((await self._conn.execute(statement)).rowcount)

    async def get_customer(self, account_id: UUID, provider: str) -> Customer | None:
        c = tables.customers
        row = (
            await self._conn.execute(
                sa.select(c).where(c.c.account_id == account_id, c.c.provider == provider)
            )
        ).first()
        return Customer(**dict(row._mapping)) if row else None

    async def find_customer(self, provider: str, provider_customer_id: str) -> Customer | None:
        c = tables.customers
        row = (
            await self._conn.execute(
                sa.select(c).where(
                    c.c.provider == provider, c.c.provider_customer_id == provider_customer_id
                )
            )
        ).first()
        return Customer(**dict(row._mapping)) if row else None

    async def save_customer(self, customer: Customer) -> None:
        await self._conn.execute(
            pg_insert(tables.customers)
            .values(
                account_id=customer.account_id,
                provider=customer.provider,
                provider_customer_id=customer.provider_customer_id,
                created_at=customer.created_at,
            )
            .on_conflict_do_nothing()
        )

    async def get_subscription(
        self, provider: str, provider_subscription_id: str
    ) -> Subscription | None:
        s = tables.subscriptions
        row = (
            await self._conn.execute(
                sa.select(s).where(
                    s.c.provider == provider,
                    s.c.provider_subscription_id == provider_subscription_id,
                )
            )
        ).first()
        return _subscription(row) if row else None

    async def save_subscription(self, subscription: Subscription) -> None:
        values = _subscription_values(subscription)
        statement = pg_insert(tables.subscriptions).values(**values)
        update = {
            k: statement.excluded[k]
            for k in values
            if k not in ("id", "provider", "provider_subscription_id", "created_at")
        }
        await self._conn.execute(
            statement.on_conflict_do_update(
                index_elements=["provider", "provider_subscription_id"], set_=update
            )
        )

    async def subscriptions_of(self, account_id: UUID) -> list[Subscription]:
        s = tables.subscriptions
        result = await self._conn.execute(
            sa.select(s).where(s.c.account_id == account_id).order_by(s.c.created_at.desc(), s.c.id)
        )
        return [_subscription(row) for row in result]

    async def save_invoice(self, invoice: Invoice) -> None:
        values = {
            "id": invoice.id,
            "account_id": invoice.account_id,
            "provider": invoice.provider,
            "provider_invoice_id": invoice.provider_invoice_id,
            "provider_subscription_id": invoice.provider_subscription_id,
            "number": invoice.number,
            "status": invoice.status,
            "currency": invoice.currency,
            "amount_due": invoice.amount_due,
            "amount_paid": invoice.amount_paid,
            "hosted_url": invoice.hosted_url,
            "pdf_url": invoice.pdf_url,
            "issued_at": invoice.issued_at,
            "updated_at": invoice.updated_at,
        }
        statement = pg_insert(tables.invoices).values(**values)
        update = {
            k: statement.excluded[k]
            for k in values
            if k not in ("id", "provider", "provider_invoice_id", "account_id")
        }
        await self._conn.execute(
            statement.on_conflict_do_update(
                index_elements=["provider", "provider_invoice_id"], set_=update
            )
        )

    async def invoices_of(self, account_id: UUID, limit: int) -> list[Invoice]:
        i = tables.invoices
        result = await self._conn.execute(
            sa.select(i)
            .where(i.c.account_id == account_id)
            .order_by(i.c.issued_at.desc(), i.c.id)
            .limit(limit)
        )
        return [Invoice(**dict(row._mapping)) for row in result]

    async def record_event(self, event: WebhookEvent) -> None:
        await self._conn.execute(
            pg_insert(tables.webhook_events)
            .values(
                provider=event.provider,
                event_id=event.event_id,
                type=event.type,
                received_at=event.received_at,
            )
            .on_conflict_do_nothing(index_elements=["provider", "event_id"])
        )

    async def claim_event(
        self, provider: str, event_id: str, now: datetime, lease_seconds: float
    ) -> bool:
        w = tables.webhook_events
        expired = now - timedelta(seconds=lease_seconds)
        result = await self._conn.execute(
            sa.update(w)
            .where(
                w.c.provider == provider,
                w.c.event_id == event_id,
                w.c.processed_at.is_(None),
                sa.or_(w.c.claimed_at.is_(None), w.c.claimed_at < expired),
            )
            .values(claimed_at=now, attempts=w.c.attempts + 1)
        )
        return bool(result.rowcount)

    async def finish_event(
        self,
        provider: str,
        event_id: str,
        outcome: EventOutcome,
        processed_at: datetime | None,
        account_id: UUID | None,
    ) -> None:
        w = tables.webhook_events
        values: dict[str, Any] = {"outcome": outcome.value, "processed_at": processed_at}
        if processed_at is None:
            values["claimed_at"] = None
        if account_id is not None:
            values["account_id"] = account_id
        await self._conn.execute(
            sa.update(w).where(w.c.provider == provider, w.c.event_id == event_id).values(**values)
        )

    async def get_event(self, provider: str, event_id: str) -> WebhookEvent | None:
        w = tables.webhook_events
        row = (
            await self._conn.execute(
                sa.select(w).where(w.c.provider == provider, w.c.event_id == event_id)
            )
        ).first()
        return _event(row) if row else None


class SqlStore:
    """Implements ``Store`` on PostgreSQL."""

    def __init__(self, engine: AsyncEngine) -> None:
        self.engine = engine

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[SqlTx]:
        """``BEGIN`` ... ``COMMIT`` (``ROLLBACK`` on error)."""
        async with self.engine.begin() as conn:
            yield SqlTx(conn)
