"""Ports: what the use cases need from the outside world, as protocols."""

from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol
from uuid import UUID

from mowdeck_contracts.v1 import ModuleDescriptor, PlanChanged

from entitlements.domain.models import AccountPlan, FeatureEntry, Plan, PlanHistory


class Clock(Protocol):
    """Returns the current time (timezone-aware)."""

    def __call__(self) -> datetime:
        """The current time."""
        ...


class Tx(Protocol):
    """One database transaction."""

    async def list_plans(self) -> list[Plan]:
        """Every plan, by key."""
        ...

    async def get_plan(self, key: str) -> Plan | None:
        """One plan."""
        ...

    async def save_plan(self, plan: Plan) -> None:
        """Insert or update a plan (by key)."""
        ...

    async def clear_default(self, except_key: str) -> None:
        """Unset ``is_default`` on every plan but ``except_key``."""
        ...

    async def get_account(self, account_id: UUID) -> AccountPlan | None:
        """The plan row of an account."""
        ...

    async def save_account(self, row: AccountPlan) -> None:
        """Insert or update an account's row."""
        ...

    async def add_account_if_absent(self, row: AccountPlan) -> bool:
        """Insert the row unless the account has one; True if inserted."""
        ...

    async def list_accounts(self, offset: int, limit: int) -> list[AccountPlan]:
        """A page of account rows, oldest first."""
        ...

    async def count_accounts(self) -> int:
        """How many accounts have a row."""
        ...

    async def add_history(self, entry: PlanHistory) -> None:
        """Append an assignment to the history."""
        ...

    async def history(self, account_id: UUID, limit: int) -> list[PlanHistory]:
        """The newest assignments of an account first."""
        ...

    async def list_features(self) -> list[FeatureEntry]:
        """The feature catalogue, by key."""
        ...

    async def save_feature(self, entry: FeatureEntry) -> None:
        """Insert or update a catalogue entry."""
        ...


class Store(Protocol):
    """Opens transactions."""

    def transaction(self) -> AbstractAsyncContextManager[Tx]:
        """A transaction: committed on normal exit, rolled back on an exception."""
        ...


@dataclass(frozen=True, slots=True)
class FetchedDescriptor:
    """The answer of one module URL (``descriptor=None``: not reachable)."""

    url: str
    descriptor: ModuleDescriptor | None


class ModuleSource(Protocol):
    """Fetches the module descriptors of the other services."""

    async def fetch(self) -> list[FetchedDescriptor]:
        """One entry per configured URL."""
        ...


class EventPublisher(Protocol):
    """Publishes ``entitlements.plan.changed``."""

    async def plan_changed(self, change: PlanChanged) -> None:
        """Publish; failures are logged, never raised (the change is committed)."""
        ...
