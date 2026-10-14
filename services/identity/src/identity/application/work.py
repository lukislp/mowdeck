"""Unit of work: one database transaction, its OpenFGA sync, its audit rows and its events.

::

    async with uow() as work:
        await work.tx.users.add(...)
        work.touch(user(anna_id))            # reconcile this subject before commit
        await work.audit(ctx, "user.created", account_id=a, target=("user", anna_id))
        work.emit(IDENTITY_USER_CREATED, a, UserChanged(...))
    # committed: events and identity.grants.changed are published now

If OpenFGA cannot be written the transaction is rolled back and :class:`AuthzUnavailable`
propagates (503). If the commit fails after OpenFGA was written, the touched subjects are
reconciled again from the committed state (best effort) before the error propagates.
"""

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from uuid import UUID

from mowdeck_authz import RelationshipTuple, Subject
from mowdeck_contracts.v1 import IDENTITY_GRANTS_CHANGED, GrantsChanged
from pydantic import BaseModel

from identity.application.catalogue import CatalogueService
from identity.application.context import RequestContext
from identity.application.ports import Clock, EventPublisher, Store, Tx
from identity.application.sync import AuthzSync, SyncResult
from identity.domain.models import AuditEntry

_log = logging.getLogger("identity.work")


@dataclass(slots=True)
class Work:
    """What one unit of work collects while it runs."""

    tx: Tx
    clock: Clock
    subjects: set[Subject] = field(default_factory=set)
    extra_tuples: list[RelationshipTuple] = field(default_factory=list)
    events: list[tuple[str, UUID | None, BaseModel]] = field(default_factory=list)
    account_id: UUID | None = None
    result: SyncResult = field(default_factory=SyncResult)

    def touch(self, *subjects: Subject) -> None:
        """Reconcile these subjects before the commit."""
        self.subjects.update(subjects)

    def ensure(self, *tuples: RelationshipTuple) -> None:
        """Write these (non-subject) tuples too, e.g. ``account#platform``."""
        self.extra_tuples.extend(tuples)

    def emit(self, event_type: str, account_id: UUID | None, payload: BaseModel) -> None:
        """Publish this event after the commit."""
        self.events.append((event_type, account_id, payload))

    async def audit(
        self,
        ctx: RequestContext,
        action: str,
        *,
        account_id: UUID | None = None,
        target: tuple[str, object] | None = None,
        details: dict[str, object] | None = None,
        outcome: str = "success",
    ) -> AuditEntry:
        """Append an audit row in this transaction."""
        entry = ctx.audit_entry(
            at=self.clock(),
            action=action,
            account_id=account_id,
            target_type=target[0] if target else None,
            target_id=str(target[1]) if target else None,
            details=dict(details or {}),
            outcome=outcome,
        )
        return await self.tx.audit.add(entry)


class UnitOfWork:
    """Factory of :class:`Work` scopes."""

    def __init__(
        self,
        store: Store,
        sync: AuthzSync,
        catalogue: CatalogueService,
        events: EventPublisher,
        clock: Clock,
    ) -> None:
        self.store = store
        self.sync = sync
        self.catalogue = catalogue
        self.events = events
        self.clock = clock

    @asynccontextmanager
    async def __call__(self) -> AsyncIterator[Work]:
        """Run a unit of work (see the module docstring)."""
        wrote = False
        work: Work | None = None
        try:
            async with self.store.transaction() as tx:
                work = Work(tx, self.clock)
                yield work
                if work.subjects or work.extra_tuples:
                    catalogue = await self.catalogue.current(tx)
                    work.result = await self.sync.reconcile(
                        tx, work.subjects, catalogue, extra_writes=work.extra_tuples
                    )
                    wrote = bool(work.result.writes or work.result.deletes)
        except Exception:
            if wrote and work is not None:
                await self._heal(work.subjects)
            raise
        await self._publish(work)

    async def _heal(self, subjects: set[Subject]) -> None:
        try:
            async with self.store.transaction() as tx:
                catalogue = await self.catalogue.current(tx)
                await self.sync.reconcile(tx, subjects, catalogue)
        except Exception:
            _log.exception(
                "healing OpenFGA after a failed commit failed; the reconcile job repairs it"
            )

    async def _publish(self, work: Work | None) -> None:
        if work is None:
            return
        for event_type, account_id, payload in work.events:
            await self.events.publish(event_type, account_id, payload)
        await publish_grants_changed(self.events, work.account_id, work.result.changed_subjects)


MAX_SUBJECTS_PER_EVENT = 500
"""Above this, ``identity.grants.changed`` asks consumers to drop their whole cache."""


async def publish_grants_changed(
    events: EventPublisher, account_id: UUID | None, subjects: set[str]
) -> None:
    """Publish the cache invalidation event for ``subjects`` (nothing if empty)."""
    if not subjects:
        return
    if len(subjects) > MAX_SUBJECTS_PER_EVENT:
        payload = GrantsChanged(account_id=account_id, all_subjects=True)
    else:
        payload = GrantsChanged(account_id=account_id, subjects=sorted(subjects))
    await events.publish(IDENTITY_GRANTS_CHANGED, account_id, payload)
