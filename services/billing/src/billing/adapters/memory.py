# SPDX-FileCopyrightText: 2026 Lukas Körber
# SPDX-License-Identifier: AGPL-3.0-only

"""In-memory store for tests and the OpenAPI export (same behaviour as the SQL store).

One transaction at a time: where the SQL store holds a row lock (``lock_customer``), this one
holds the whole store, which gives the same order between an event that is being applied and
the erasure of its account.
"""

import asyncio
import copy
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from uuid import UUID

from mowdeck_contracts.v1 import SubscriptionStatus

from billing.domain.ledger import FactKind, PaymentFact, apply_update
from billing.domain.models import (
    BillingAccount,
    Customer,
    EventOutcome,
    Invoice,
    ManualRefund,
    Subscription,
    WebhookEvent,
)
from billing.domain.statistics import OpenInvoice, SubscriptionTally


@dataclass
class _Data:
    accounts: dict[UUID, BillingAccount] = field(default_factory=dict)
    customers: dict[tuple[UUID, str], Customer] = field(default_factory=dict)
    subscriptions: dict[tuple[str, str], Subscription] = field(default_factory=dict)
    invoices: dict[tuple[str, str], Invoice] = field(default_factory=dict)
    events: dict[tuple[str, str], WebhookEvent] = field(default_factory=dict)
    manual_refunds: dict[UUID, ManualRefund] = field(default_factory=dict)
    facts: dict[UUID, PaymentFact] = field(default_factory=dict)


class MemoryTx:
    """``Tx`` on a private copy; the store commits it on success."""

    def __init__(self, data: _Data) -> None:
        self.data = data

    async def get_account(self, account_id: UUID) -> BillingAccount | None:
        found = self.data.accounts.get(account_id)
        return replace(found) if found else None

    async def save_account_if_absent(self, account: BillingAccount) -> bool:
        if account.account_id in self.data.accounts:
            return False
        self.data.accounts[account.account_id] = replace(account)
        return True

    async def get_customer(self, account_id: UUID, provider: str) -> Customer | None:
        found = self.data.customers.get((account_id, provider))
        return replace(found) if found else None

    async def find_customer(self, provider: str, provider_customer_id: str) -> Customer | None:
        for customer in self.data.customers.values():
            if (customer.provider, customer.provider_customer_id) == (
                provider,
                provider_customer_id,
            ):
                return replace(customer)
        return None

    async def lock_customer(self, provider: str, provider_customer_id: str) -> Customer | None:
        return await self.find_customer(provider, provider_customer_id)

    async def customers_of(self, account_id: UUID) -> list[Customer]:
        found = [replace(c) for c in self.data.customers.values() if c.account_id == account_id]
        return sorted(found, key=lambda c: c.provider)

    async def save_customer(self, customer: Customer) -> None:
        key = (customer.account_id, customer.provider)
        taken = await self.find_customer(customer.provider, customer.provider_customer_id)
        if key not in self.data.customers and taken is None:
            self.data.customers[key] = replace(customer)

    async def get_subscription(
        self, provider: str, provider_subscription_id: str
    ) -> Subscription | None:
        found = self.data.subscriptions.get((provider, provider_subscription_id))
        return replace(found) if found else None

    async def save_subscription(self, subscription: Subscription) -> None:
        key = (subscription.provider, subscription.provider_subscription_id)
        current = self.data.subscriptions.get(key)
        stored = replace(subscription)
        if current is not None:
            stored.id, stored.created_at = current.id, current.created_at
        self.data.subscriptions[key] = stored

    async def subscriptions_of(self, account_id: UUID) -> list[Subscription]:
        found = [replace(s) for s in self.data.subscriptions.values() if s.account_id == account_id]
        return sorted(found, key=lambda s: (-s.created_at.timestamp(), str(s.id)))

    async def save_invoice(self, invoice: Invoice) -> None:
        key = (invoice.provider, invoice.provider_invoice_id)
        current = self.data.invoices.get(key)
        stored = replace(invoice)
        if current is not None:
            stored.id, stored.account_id = current.id, current.account_id
        self.data.invoices[key] = stored

    async def invoices_of(self, account_id: UUID, limit: int) -> list[Invoice]:
        found = [replace(i) for i in self.data.invoices.values() if i.account_id == account_id]
        found.sort(key=lambda i: (-i.issued_at.timestamp(), str(i.id)))
        return found[:limit]

    async def record_event(self, event: WebhookEvent) -> None:
        self.data.events.setdefault((event.provider, event.event_id), replace(event))

    async def claim_event(
        self, provider: str, event_id: str, now: datetime, lease_seconds: float
    ) -> bool:
        event = self.data.events.get((provider, event_id))
        if event is None or event.processed_at is not None:
            return False
        if event.claimed_at is not None and event.claimed_at >= now - timedelta(
            seconds=lease_seconds
        ):
            return False
        event.claimed_at = now
        event.attempts += 1
        return True

    async def attribute_event(self, provider: str, event_id: str, account_id: UUID) -> None:
        event = self.data.events.get((provider, event_id))
        if event is not None:
            event.account_id = account_id

    async def finish_event(
        self,
        provider: str,
        event_id: str,
        outcome: EventOutcome,
        processed_at: datetime | None,
    ) -> None:
        event = self.data.events.get((provider, event_id))
        if event is None:
            return
        event.outcome = outcome
        event.processed_at = processed_at
        if processed_at is None:
            event.claimed_at = None

    async def get_event(self, provider: str, event_id: str) -> WebhookEvent | None:
        found = self.data.events.get((provider, event_id))
        return replace(found) if found else None

    async def purge_account(self, account_id: UUID) -> int:
        removed = sum(self._account_rows(account_id).values())
        data = self.data
        data.accounts.pop(account_id, None)
        data.customers = {k: v for k, v in data.customers.items() if v.account_id != account_id}
        data.subscriptions = {
            k: v for k, v in data.subscriptions.items() if v.account_id != account_id
        }
        data.invoices = {k: v for k, v in data.invoices.items() if v.account_id != account_id}
        data.events = {k: v for k, v in data.events.items() if v.account_id != account_id}
        return removed

    async def add_manual_refund(self, refund: ManualRefund) -> bool:
        if any(r.key == refund.key for r in self.data.manual_refunds.values()):
            return False
        self.data.manual_refunds[refund.id] = replace(refund)
        return True

    async def manual_refunds(self, *, settled: bool) -> list[ManualRefund]:
        found = [replace(r) for r in self.data.manual_refunds.values() if settled or r.is_open]
        return sorted(found, key=lambda r: (r.created_at, str(r.id)))

    async def settle_manual_refund(self, refund_id: UUID, at: datetime) -> ManualRefund | None:
        found = self.data.manual_refunds.get(refund_id)
        if found is None:
            return None
        if found.settled_at is None:
            found.settled_at = at
        return replace(found)

    async def delete_settled_manual_refunds(self, before: datetime) -> int:
        gone = [
            r.id
            for r in self.data.manual_refunds.values()
            if r.settled_at is not None and r.settled_at < before
        ]
        for refund_id in gone:
            del self.data.manual_refunds[refund_id]
        return len(gone)

    async def open_manual_refunds(self) -> int:
        return sum(1 for r in self.data.manual_refunds.values() if r.is_open)

    async def account_rows(self, account_id: UUID) -> dict[str, int]:
        return {table: found for table, found in self._account_rows(account_id).items() if found}

    async def record_fact(self, fact: PaymentFact) -> bool:
        facts = self.data.facts
        stored = next(
            (
                f
                for f in facts.values()
                if fact.ref is not None and (f.kind, f.ref) == (fact.kind, fact.ref)
            ),
            None,
        )
        if stored is None:
            facts[fact.id] = replace(fact)
            return True
        updated = apply_update(stored, fact)
        if updated is None:
            return False
        facts[stored.id] = updated
        return True

    async def payment_facts(self, since: datetime) -> list[PaymentFact]:
        def wanted(f: PaymentFact) -> bool:
            if f.occurred_at >= since:
                return True
            return f.kind is FactKind.DISPUTE and (f.closed_at is None or f.closed_at >= since)

        found = [replace(f) for f in self.data.facts.values() if wanted(f)]
        return sorted(found, key=lambda f: (f.occurred_at, str(f.id)))

    async def first_fact_at(self) -> datetime | None:
        return min((f.recorded_at for f in self.data.facts.values()), default=None)

    async def forget_fact_refs(self, before: datetime) -> int:
        cleared = 0
        for fact in self.data.facts.values():
            if fact.ref is not None and fact.updated_at < before:
                fact.ref = None
                cleared += 1
        return cleared

    async def subscription_tally(self) -> list[SubscriptionTally]:
        counts: dict[tuple[str | None, SubscriptionStatus], int] = {}
        for s in self.data.subscriptions.values():
            counts[(s.plan_key, s.status)] = counts.get((s.plan_key, s.status), 0) + 1
        return [
            SubscriptionTally(plan, status, count)
            for (plan, status), count in sorted(
                counts.items(), key=lambda item: (item[0][0] or "", item[0][1].value)
            )
        ]

    async def open_invoices(self) -> list[OpenInvoice]:
        found = sorted(
            (i for i in self.data.invoices.values() if i.status == "open"),
            key=lambda i: (i.issued_at, str(i.id)),
        )
        return [
            OpenInvoice(i.currency, max(i.amount_due - i.amount_paid, 0), i.issued_at)
            for i in found
        ]

    def _account_rows(self, account_id: UUID) -> dict[str, int]:
        data = self.data
        return {
            "accounts": int(account_id in data.accounts),
            "customers": sum(1 for c in data.customers.values() if c.account_id == account_id),
            "subscriptions": sum(
                1 for s in data.subscriptions.values() if s.account_id == account_id
            ),
            "invoices": sum(1 for i in data.invoices.values() if i.account_id == account_id),
            "webhook_events": sum(1 for e in data.events.values() if e.account_id == account_id),
        }


class MemoryStore:
    """Implements ``Store`` in memory; a transaction works on a copy and commits on success.
    One transaction at a time, like the row locks of the SQL store but coarser."""

    def __init__(self) -> None:
        self.data = _Data()
        self._lock = asyncio.Lock()

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[MemoryTx]:
        """Copy, yield, and keep the copy unless the block raised."""
        async with self._lock:
            working = copy.deepcopy(self.data)
            yield MemoryTx(working)
            self.data = working
