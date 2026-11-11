# SPDX-FileCopyrightText: 2026 Lukas Körber
# SPDX-License-Identifier: AGPL-3.0-only

"""The stored domain events of an erased account or a deleted user (ADR 0082).

The ``EVENTS`` stream keeps every domain event for 30 days: notification titles and bodies,
mower names, tracking positions, the name of whoever asked for something. Its subjects
(``event.<domain>.<name>``) name neither an account nor a user, so nothing of one account can be
purged by a subject filter: the account is in the envelope. Identity owns the accounts and the
users, so Identity removes their events.

* :class:`BusEventLog` is the port :class:`~identity.application.ports.EventLog` over the kit's
  :class:`~mowdeck_service_kit.bus.StreamStore`: it reads a subject filter between two
  sequences (in batches, at the reader's pace), decodes the envelopes and erases single
  messages. It holds no rule and no position.
* :func:`subscribe_user_events` hands ``event.identity.user.deleted`` (durable
  ``identity-user-events``) to the use case, which records the work and runs its first slice
  (:class:`~identity.application.event_erasure.EventErasureService`). The delivery is
  acknowledged once the record exists; the read itself is not tied to it.

A message that is not a JSON object is nobody's event and stays.
"""

import json
from collections.abc import AsyncGenerator, Awaitable, Callable
from uuid import UUID

from mowdeck_contracts.v1 import IDENTITY_USER_DELETED, EventEnvelope, UserChanged
from mowdeck_contracts.v1.subjects import EVENTS, event_subject_for_type, event_wildcard
from mowdeck_service_kit.bus import (
    BusMessage,
    DropMessage,
    EventBus,
    MessageHandler,
    StoredMessage,
    StreamStore,
    Subscription,
)
from pydantic import ValidationError

from identity.application.ports import StoredEvent

USER_EVENTS_CONSUMER = "identity-user-events"
"""Durable consumer (and deliver group) of ``event.identity.user.deleted`` that starts the removal
of the deleted user's stored events."""

UserDeleted = Callable[[UUID], Awaitable[object]]
"""What the handler calls with the id of a deleted user."""


class BusEventLog:
    """:class:`~identity.application.ports.EventLog` on the bus's ``EVENTS`` stream.

    Without a bus, or with one that keeps no streams, there are no stored events: the stream
    ends at 0 and nothing is read.
    """

    def __init__(self, bus: EventBus | None) -> None:
        self._store: StreamStore | None = bus if isinstance(bus, StreamStore) else None

    async def end(self) -> int:
        """The newest sequence of ``EVENTS`` now."""
        if self._store is None:
            return 0
        return await self._store.last_sequence(event_wildcard())

    async def read(self, subject: str, after: int, until: int) -> AsyncGenerator[StoredEvent]:
        """The stored events on ``subject`` above ``after`` and up to ``until``."""
        store = self._store
        if store is None or not EVENTS.captures(subject.replace("*", "x").replace(">", "x")):
            return
        async for message in store.stored_messages(subject, after=after, until=until):
            try:
                event = json.loads(message.data)
            except ValueError:
                event = None
            yield StoredEvent(
                sequence=message.sequence,
                event=event if isinstance(event, dict) else None,
                ref=message,
            )

    async def erase(self, event: StoredEvent) -> bool:
        """Remove one stored event (the server overwrites it)."""
        if self._store is None or not isinstance(event.ref, StoredMessage):
            return False
        return await self._store.erase_message(event.ref)


def user_deleted_handler(deleted: UserDeleted) -> MessageHandler:
    """Start the removal of a deleted user's stored events. Raises when the work could not be
    recorded (the event comes again); an event that cannot be decoded is dropped."""

    async def handle(message: BusMessage) -> None:
        try:
            event = message.model(EventEnvelope[UserChanged])
        except ValidationError as exc:
            raise DropMessage(
                f"invalid identity.user.deleted event: {exc.error_count()} errors"
            ) from exc
        if event.type != IDENTITY_USER_DELETED:
            return
        await deleted(event.payload.user_id)

    return handle


async def subscribe_user_events(bus: EventBus, deleted: UserDeleted) -> Subscription:
    """Remove a user's stored events whenever a user is deleted."""
    return await bus.subscribe(
        event_subject_for_type(IDENTITY_USER_DELETED),
        user_deleted_handler(deleted),
        durable=USER_EVENTS_CONSUMER,
        queue=USER_EVENTS_CONSUMER,
    )
