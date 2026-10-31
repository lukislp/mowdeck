"""Reading the audit log of an account (newest first, keyset pagination by id).

**Public demo** (ADR 0068): every visitor signs in as the one demo user, which holds
``audit.view``, so the account's log would list every visitor's sign-ins with their addresses
and sessions. In demo mode a reader only gets the rows of their own login session (``details``
``session``), and never an address.
"""

from dataclasses import dataclass, replace
from datetime import datetime
from typing import Any
from uuid import UUID

from mowdeck_contracts.v1 import Principal

from identity.application.ports import Ge, Lt, Store
from identity.domain.models import AuditEntry

MAX_PAGE = 200

DEMO_SCAN_MAX = 5000
"""Rows a demo query looks through for the reader's own session before it stops (the demo
resets every few hours, so its log stays small)."""


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

    def __init__(self, store: Store, *, demo: bool = False) -> None:
        self.store = store
        self.demo = demo
        """Demo mode: only the reader's own login session, without addresses."""

    async def query(
        self, account_id: UUID, query: AuditQuery, reader: Principal | None = None
    ) -> list[AuditEntry]:
        """Matching entries of the account, newest first (in demo mode only those of the
        ``reader``'s login session, without the address)."""
        filters: dict[str, Any] = {"account_id": account_id}
        if query.action:
            filters["action"] = query.action
        if query.actor:
            filters["actor_subject"] = query.actor
        if query.target_id:
            filters["target_id"] = query.target_id
        if query.since:
            filters["at"] = Ge(query.since)
        limit = max(1, min(query.limit, MAX_PAGE))
        if self.demo:
            session = reader.session_id if reader is not None else None
            return [] if session is None else await self._own(filters, query, limit, session)
        if query.before_id is not None:
            filters["id"] = Lt(query.before_id)
        async with self.store.transaction() as tx:
            return await tx.audit.find(order_by="id", descending=True, limit=limit, **filters)

    async def _own(
        self, filters: dict[str, Any], query: AuditQuery, limit: int, session: UUID
    ) -> list[AuditEntry]:
        """The newest ``limit`` rows of one login session, addresses removed."""
        wanted = str(session)
        found: list[AuditEntry] = []
        before = query.before_id
        scanned = 0
        async with self.store.transaction() as tx:
            while len(found) < limit and scanned < DEMO_SCAN_MAX:
                page_filters = dict(filters)
                if before is not None:
                    page_filters["id"] = Lt(before)
                page = await tx.audit.find(
                    order_by="id", descending=True, limit=MAX_PAGE, **page_filters
                )
                found.extend(e for e in page if (e.details or {}).get("session") == wanted)
                scanned += len(page)
                if len(page) < MAX_PAGE or page[-1].id is None:
                    break
                before = page[-1].id
        return [replace(entry, ip=None) for entry in found[:limit]]
