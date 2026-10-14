"""Reading the audit log of an account (newest first, keyset pagination by id)."""

from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

from identity.application.ports import Ge, Lt, Store
from identity.domain.models import AuditEntry

MAX_PAGE = 200


@dataclass(frozen=True, slots=True)
class AuditQuery:
    """Filters of the audit query."""

    action: str | None = None
    actor: str | None = None
    target_id: str | None = None
    since: datetime | None = None
    before_id: int | None = None
    limit: int = 50


class AuditService:
    """Audit queries; always filtered by account."""

    def __init__(self, store: Store) -> None:
        self.store = store

    async def query(self, account_id: UUID, query: AuditQuery) -> list[AuditEntry]:
        """Matching entries of the account, newest first."""
        filters: dict[str, Any] = {"account_id": account_id}
        if query.action:
            filters["action"] = query.action
        if query.actor:
            filters["actor_subject"] = query.actor
        if query.target_id:
            filters["target_id"] = query.target_id
        if query.since:
            filters["at"] = Ge(query.since)
        if query.before_id is not None:
            filters["id"] = Lt(query.before_id)
        limit = max(1, min(query.limit, MAX_PAGE))
        async with self.store.transaction() as tx:
            return await tx.audit.find(order_by="id", descending=True, limit=limit, **filters)
