# SPDX-FileCopyrightText: 2026 Lukas Körber
# SPDX-License-Identifier: AGPL-3.0-only

"""Removing the stored domain events of an erased account or a deleted user (ADR 0082).

The ``EVENTS`` stream keeps every account's events for 30 days and its subjects name no account,
so Identity has to read the stream to find the ones that belong to somebody. On an installation
with many accounts that is a long read. This use case makes it finish at any stream size:

* **A record, not a handler.** The work is an :class:`~identity.domain.erasure.EventErasure`
  row. ``identity.user.deleted`` only writes the row (:meth:`EventErasureService.request`) and
  runs one slice; nothing holds a delivery open while the stream is read, so nothing is
  redelivered and started over.
* **Slices.** :meth:`EventErasureService.advance` reads for at most ``budget.seconds`` or
  ``budget.messages``, stores how far it got and returns. The purge of an account calls it from
  its sweep and comes back for the next slice, so one long read neither outlives the purge's
  lease nor keeps other accounts waiting; a background loop does the same for users.
* **Resumable.** The row remembers the filter and the sequence it reached: a restart, or a
  failure in the middle, continues there.
* **It ends.** The row remembers the stream's last sequence when the work began and never reads
  past it. Events published later cannot be the account's (every service confirmed first) and
  not the user's (the user is gone), so traffic cannot keep the read alive.
* **Only what can name somebody.** The subjects read are
  :data:`~identity.domain.erasure.EVENT_FILTERS`; events that carry ids only are skipped.

Which event belongs to whom is the domain's rule; the stream is behind the port
:class:`~identity.application.ports.EventLog`.
"""

import asyncio
import time
from collections.abc import Callable
from contextlib import aclosing
from dataclasses import dataclass
from uuid import UUID

from mowdeck_service_kit import get_logger

from identity.application.deps import Deps
from identity.application.ports import EventLog
from identity.domain.erasure import EventErasure, EventScope

_log = get_logger("identity.event_erasure")


@dataclass(frozen=True, slots=True)
class EventBudget:
    """How much one slice may do before it stores its position and returns."""

    seconds: float = 10.0
    """Well below the 30 s a bus delivery may take and the five minutes a purge attempt is
    leased for."""
    messages: int = 100_000


class EventErasureService:
    """Erases the stored events of accounts and users, a slice at a time."""

    def __init__(
        self,
        deps: Deps,
        log: EventLog,
        budget: EventBudget | None = None,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self.deps = deps
        self.log = log
        self.budget = budget or EventBudget()
        self._monotonic = monotonic

    async def request(self, scope: EventScope, subject_id: UUID) -> EventErasure:
        """The record of the work for ``subject_id``, created when there is none: it reads up
        to the stream's last sequence of now. Idempotent."""
        async with self.deps.store.transaction() as tx:
            found = await tx.event_erasures.get(subject_id=subject_id)
        if found is not None:
            return found
        record = EventErasure.begin(scope, subject_id, self.deps.clock(), await self.log.end())
        async with self.deps.store.transaction() as tx:
            if await tx.event_erasures.get(subject_id=subject_id) is None:
                await tx.event_erasures.add(record)
        return record

    async def advance(self, scope: EventScope, subject_id: UUID) -> int | None:
        """Run one slice (the record is created on the first call). Returns how many events
        were erased in all when the work is finished (the record is deleted then), None when
        more slices are needed. Raises when the stream could not be read or changed; what the
        slice did until then is stored."""
        record = await self.request(scope, subject_id)
        started = self._monotonic()
        handled = 0
        try:
            while not record.finished:
                events = self.log.read(record.filter, record.scanned_sequence, record.last_sequence)
                async with aclosing(events) as stream:
                    async for stored in stream:
                        names = stored.event is not None and record.names(stored.event)
                        if names and await self.log.erase(stored):
                            record.erased += 1
                        record.scanned(stored.sequence)
                        handled += 1
                        spent = self._monotonic() - started
                        if handled >= self.budget.messages or spent >= self.budget.seconds:
                            return None
                record.next_filter()
        finally:
            await self._save(record)
        _log.info(
            "stored events erased",
            scope=record.scope,
            subject_id=str(subject_id),
            erased=record.erased,
        )
        return record.erased

    async def _save(self, record: EventErasure) -> None:
        async with self.deps.store.transaction() as tx:
            if record.finished:
                await tx.event_erasures.delete(subject_id=record.subject_id)
            elif await tx.event_erasures.get(subject_id=record.subject_id) is not None:
                await tx.event_erasures.update(record)

    # --- users --------------------------------------------------------------------------------

    async def pending_users(self) -> list[EventErasure]:
        """The deleted users whose events are not all read yet, oldest first."""
        async with self.deps.store.transaction() as tx:
            return await tx.event_erasures.find(
                scope=EventScope.USER.value, order_by="requested_at"
            )

    async def sweep_users(self) -> tuple[int, int]:
        """One slice for every deleted user with open work: ``(still open, failed)``. A failure
        is logged and tried again with a later sweep (the position is kept)."""
        still_open = failed = 0
        for record in await self.pending_users():
            try:
                if await self.advance(EventScope.USER, record.subject_id) is None:
                    still_open += 1
            except Exception:
                failed += 1
                _log.exception(
                    "erasing a deleted user's stored events failed, retrying later",
                    user_id=str(record.subject_id),
                )
        return still_open, failed

    async def run(self, every_seconds: float = 5.0) -> None:
        """Carry on the work for deleted users: at once while slices are left, else (and after
        a failure) every ``every_seconds``."""
        while True:
            try:
                still_open, failed = await self.sweep_users()
            except Exception:
                _log.exception("the sweep of stored user events failed")
                still_open, failed = 0, 1
            await asyncio.sleep(0 if still_open and not failed else every_seconds)
