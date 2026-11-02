"""In-memory store (tests and local experiments) with the semantics of the PostgreSQL store:
transactions are all or nothing (a snapshot is restored on error) and run one at a time."""

import asyncio
import copy
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime
from uuid import UUID

from planner.domain.models import MowerFacts, Template


@dataclass
class MemoryData:
    """All rows of the memory store."""

    facts: dict[UUID, MowerFacts] = field(default_factory=dict)
    templates: dict[tuple[UUID, str], Template] = field(default_factory=dict)


class MemoryTx:
    """``Tx`` over the memory data."""

    def __init__(self, data: MemoryData) -> None:
        self._data = data

    async def get_facts(self, mower_id: UUID) -> MowerFacts | None:
        found = self._data.facts.get(mower_id)
        return copy.deepcopy(found) if found else None

    def _put(
        self,
        kind: str,
        mower_id: UUID,
        account_id: UUID,
        observed_at: datetime,
        value: dict[str, object],
    ) -> bool:
        facts = self._data.facts.get(mower_id) or MowerFacts(mower_id, account_id)
        stamp = getattr(facts, f"{kind}_observed_at")
        if stamp is not None and stamp > observed_at:
            return False
        facts.account_id = account_id
        setattr(facts, kind, copy.deepcopy(value))
        setattr(facts, f"{kind}_observed_at", observed_at)
        self._data.facts[mower_id] = facts
        return True

    async def put_map(
        self, mower_id: UUID, account_id: UUID, observed_at: datetime, value: dict[str, object]
    ) -> bool:
        return self._put("map", mower_id, account_id, observed_at, value)

    async def put_capabilities(
        self, mower_id: UUID, account_id: UUID, observed_at: datetime, value: dict[str, object]
    ) -> bool:
        return self._put("capabilities", mower_id, account_id, observed_at, value)

    async def list_templates(self, account_id: UUID) -> list[Template]:
        rows = [t for (acc, _), t in self._data.templates.items() if acc == account_id]
        return [copy.deepcopy(t) for t in sorted(rows, key=lambda t: t.name)]

    async def get_template(self, account_id: UUID, name: str) -> Template | None:
        found = self._data.templates.get((account_id, name))
        return copy.deepcopy(found) if found else None

    async def count_templates(self, account_id: UUID) -> int:
        return sum(1 for acc, _ in self._data.templates if acc == account_id)

    async def put_template(self, template: Template) -> None:
        self._data.templates[(template.account_id, template.name)] = copy.deepcopy(template)

    async def delete_template(self, account_id: UUID, name: str) -> bool:
        return self._data.templates.pop((account_id, name), None) is not None

    async def purge_account(self, account_id: UUID) -> int:
        templates = [key for key in self._data.templates if key[0] == account_id]
        mowers = [m for m, facts in self._data.facts.items() if facts.account_id == account_id]
        for key in templates:
            del self._data.templates[key]
        for mower_id in mowers:
            del self._data.facts[mower_id]
        return len(templates) + len(mowers)


class MemoryStore:
    """``Store`` in memory."""

    def __init__(self) -> None:
        self.data = MemoryData()
        self._lock = asyncio.Lock()

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[MemoryTx]:
        """A serialised transaction; the data is restored when the block raises."""
        async with self._lock:
            snapshot = copy.deepcopy(self.data)
            try:
                yield MemoryTx(self.data)
            except BaseException:
                self.data = snapshot
                raise
