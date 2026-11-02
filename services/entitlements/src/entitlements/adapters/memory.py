"""In-memory ``Store`` for tests and development (same contract as ``SqlStore``).

A transaction works on copies and replaces the state on commit, so a failing use case leaves
nothing behind (like a rolled back database transaction).
"""

import asyncio
import copy
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from uuid import UUID

from entitlements.domain.models import AccountPlan, FeatureEntry, Plan, PlanHistory


@dataclass
class _State:
    plans: dict[str, Plan] = field(default_factory=dict)
    accounts: dict[UUID, AccountPlan] = field(default_factory=dict)
    history: list[PlanHistory] = field(default_factory=list)
    features: dict[str, FeatureEntry] = field(default_factory=dict)


class MemoryTx:
    """A transaction over a copy of the state."""

    def __init__(self, state: _State) -> None:
        self.state = state

    async def list_plans(self) -> list[Plan]:
        """Every plan, by key."""
        return [copy.deepcopy(p) for _, p in sorted(self.state.plans.items())]

    async def get_plan(self, key: str) -> Plan | None:
        """One plan."""
        found = self.state.plans.get(key)
        return copy.deepcopy(found) if found else None

    async def save_plan(self, plan: Plan) -> None:
        """Insert or update."""
        self.state.plans[plan.key] = copy.deepcopy(plan)

    async def clear_default(self, except_key: str) -> None:
        """Unset ``is_default`` on the others."""
        for key, plan in self.state.plans.items():
            if key != except_key:
                plan.is_default = False

    async def get_account(self, account_id: UUID) -> AccountPlan | None:
        """The row of an account."""
        found = self.state.accounts.get(account_id)
        return copy.deepcopy(found) if found else None

    async def save_account(self, row: AccountPlan) -> None:
        """Insert or update."""
        self.state.accounts[row.account_id] = copy.deepcopy(row)

    async def add_account_if_absent(self, row: AccountPlan) -> bool:
        """Insert unless present."""
        if row.account_id in self.state.accounts:
            return False
        self.state.accounts[row.account_id] = copy.deepcopy(row)
        return True

    async def list_accounts(self, offset: int, limit: int) -> list[AccountPlan]:
        """A page, oldest first (then by id)."""
        rows = sorted(self.state.accounts.values(), key=lambda r: (r.since, str(r.account_id)))
        return [copy.deepcopy(r) for r in rows[offset : offset + limit]]

    async def count_accounts(self) -> int:
        """How many rows."""
        return len(self.state.accounts)

    async def add_history(self, entry: PlanHistory) -> None:
        """Append."""
        self.state.history.append(copy.deepcopy(entry))

    async def history(self, account_id: UUID, limit: int) -> list[PlanHistory]:
        """Newest first."""
        own = [h for h in reversed(self.state.history) if h.account_id == account_id]
        own.sort(key=lambda h: h.at, reverse=True)
        return [copy.deepcopy(h) for h in own[:limit]]

    async def list_features(self) -> list[FeatureEntry]:
        """The catalogue, by key."""
        return [copy.deepcopy(f) for _, f in sorted(self.state.features.items())]

    async def save_feature(self, entry: FeatureEntry) -> None:
        """Insert or update."""
        self.state.features[entry.key] = copy.deepcopy(entry)

    async def purge_account(self, account_id: UUID) -> int:
        """Drop the account's row and history."""
        before = len(self.state.accounts) + len(self.state.history)
        self.state.accounts.pop(account_id, None)
        self.state.history = [h for h in self.state.history if h.account_id != account_id]
        return before - len(self.state.accounts) - len(self.state.history)


class MemoryStore:
    """The state lives in this object; one transaction at a time."""

    def __init__(self) -> None:
        self.state = _State()
        self._lock = asyncio.Lock()

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[MemoryTx]:
        """Work on a copy; replace the state on success."""
        async with self._lock:
            working = copy.deepcopy(self.state)
            yield MemoryTx(working)
            self.state = working
