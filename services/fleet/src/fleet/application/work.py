"""Unit of work: one database transaction, its OpenFGA tuples, its audit rows and its events.

::

    async with uow() as work:
        await work.tx.mowers.add(mower)
        work.write(RelationshipTuple(account(a).as_subject(), "account", mower_ref))
        await work.audit(actor, "mower.registered", a, "mower", mower.id)
        work.emit(MOWER_REGISTERED, a, registration)
    # committed; the event is published now

Same consistency rule as Identity (no outbox): the tuples are written **inside** the database
transaction, right before the commit, through ``TupleWriter(owner="fleet")``. If OpenFGA cannot
be written the transaction rolls back and :class:`~fleet.domain.errors.Unavailable` propagates
(503): nothing is half applied, and no event is published. If the commit fails after the
tuples were written, the change is undone in OpenFGA (best effort, logged) before the error
propagates. Events are published only after the commit.
"""

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

from mowdeck_authz import RelationshipTuple, TupleChange, TupleOwnershipError, TupleWriter
from pydantic import BaseModel

from fleet.application.context import Actor
from fleet.application.ports import Clock, EventPublisher, Store, Tx
from fleet.domain.errors import Unavailable
from fleet.domain.models import AuditEntry

_log = logging.getLogger("fleet.work")


@dataclass(slots=True)
class Work:
    """What one unit of work collects while it runs."""

    tx: Tx
    clock: Clock
    writes: list[RelationshipTuple] = field(default_factory=list)
    deletes: list[RelationshipTuple] = field(default_factory=list)
    events: list[tuple[str, UUID, BaseModel]] = field(default_factory=list)

    def write(self, *tuples: RelationshipTuple) -> None:
        """Write these tuples before the commit."""
        self.writes.extend(tuples)

    def delete(self, *tuples: RelationshipTuple) -> None:
        """Delete these tuples before the commit."""
        self.deletes.extend(tuples)

    def emit(self, event_type: str, account_id: UUID, payload: BaseModel) -> None:
        """Publish this event after the commit."""
        self.events.append((event_type, account_id, payload))

    async def audit(
        self,
        actor: Actor,
        action: str,
        account_id: UUID,
        target_type: str,
        target_id: object,
        details: dict[str, Any] | None = None,
    ) -> None:
        """Append an audit row in this transaction."""
        await self.tx.audit.add(
            AuditEntry(
                at=self.clock(),
                action=action,
                account_id=account_id,
                target_type=target_type,
                target_id=str(target_id),
                actor_subject=actor.subject,
                actor_kind=actor.principal.kind.value if actor.principal else None,
                details=dict(details or {}),
                correlation_id=actor.correlation_id,
            )
        )


class UnitOfWork:
    """Factory of :class:`Work` scopes."""

    def __init__(
        self, store: Store, writer: TupleWriter, events: EventPublisher, clock: Clock
    ) -> None:
        self.store = store
        self.writer = writer
        self.events = events
        self.clock = clock

    @asynccontextmanager
    async def __call__(self) -> AsyncIterator[Work]:
        """Run a unit of work (see the module docstring)."""
        applied: TupleChange | None = None
        work: Work | None = None
        try:
            async with self.store.transaction() as tx:
                work = Work(tx, self.clock)
                yield work
                if work.writes or work.deletes:
                    applied = await self._apply(work)
        except Exception:
            if applied is not None and not applied.is_empty:
                await self._undo(applied)
            raise
        for event_type, account_id, payload in work.events:
            await self.events.publish(event_type, account_id, payload)

    async def _apply(self, work: Work) -> TupleChange:
        try:
            return await self.writer.write(work.writes, work.deletes)
        except TupleOwnershipError:
            raise  # a programming error, not an outage
        except Exception as exc:
            _log.warning("writing tuples failed", extra={"error": type(exc).__name__})
            msg = "the authorization service is unavailable; nothing was changed"
            raise Unavailable(msg) from exc

    async def _undo(self, change: TupleChange) -> None:
        try:
            await self.writer.write(adds=change.deletes, deletes=change.writes)
        except Exception:
            _log.exception("undoing tuples after a failed commit failed; OpenFGA may have drifted")
