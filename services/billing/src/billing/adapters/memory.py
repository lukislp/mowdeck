"""In-memory store for tests and the OpenAPI export (same behaviour as the SQL store)."""

import copy
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from uuid import UUID

from billing.domain.models import (
    BillingAccount,
    Customer,
    EventOutcome,
    Invoice,
    Subscription,
    WebhookEvent,
)


@dataclass
class _Data:
    accounts: dict[UUID, BillingAccount] = field(default_factory=dict)
    customers: dict[tuple[UUID, str], Customer] = field(default_factory=dict)
    subscriptions: dict[tuple[str, str], Subscription] = field(default_factory=dict)
    invoices: dict[tuple[str, str], Invoice] = field(default_factory=dict)
    events: dict[tuple[str, str], WebhookEvent] = field(default_factory=dict)


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

    async def finish_event(
        self,
        provider: str,
        event_id: str,
        outcome: EventOutcome,
        processed_at: datetime | None,
        account_id: UUID | None,
    ) -> None:
        event = self.data.events.get((provider, event_id))
        if event is None:
            return
        event.outcome = outcome
        event.processed_at = processed_at
        if processed_at is None:
            event.claimed_at = None
        if account_id is not None:
            event.account_id = account_id

    async def get_event(self, provider: str, event_id: str) -> WebhookEvent | None:
        found = self.data.events.get((provider, event_id))
        return replace(found) if found else None


class MemoryStore:
    """Implements ``Store`` in memory; a transaction works on a copy and commits on success."""

    def __init__(self) -> None:
        self.data = _Data()

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[MemoryTx]:
        """Copy, yield, and keep the copy unless the block raised."""
        working = copy.deepcopy(self.data)
        yield MemoryTx(working)
        self.data = working
