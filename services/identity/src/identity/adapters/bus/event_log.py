# SPDX-FileCopyrightText: 2026 Lukas Körber
# SPDX-License-Identifier: AGPL-3.0-only

"""The stored domain events of an erased account or a deleted user (ADR 0082).

The ``EVENTS`` stream keeps every domain event for 30 days: notification titles and bodies,
mower names, tracking positions, the name of whoever asked for something. Its subjects
(``event.<domain>.<name>``) name neither an account nor a user, so nothing of one account can be
purged by a subject filter: the account is in the envelope. Identity owns the accounts and the
users, so Identity removes their events: :class:`BusEventLog` reads the stream message by message
(the kit's :class:`~mowdeck_service_kit.bus.StreamStore`), decides with the rules of
:mod:`identity.domain.erasure` and erases what names the account or the user.

* The account erasure calls :meth:`BusEventLog.forget_account` after the last service confirmed
  and before Identity deletes its own rows; a failure keeps the purge open.
* :func:`subscribe_user_events` calls :meth:`BusEventLog.forget_user` on
  ``event.identity.user.deleted`` (durable ``identity-user-events``), for every deleted or
  removed user; a failure is delivered again.

A message that is not a JSON object is nobody's event and stays.
"""

import json
from collections.abc import Callable, Mapping
from typing import Any
from uuid import UUID

from mowdeck_contracts.v1 import IDENTITY_USER_DELETED, EventEnvelope, UserChanged
from mowdeck_contracts.v1.subjects import event_subject_for_type, event_wildcard
from mowdeck_service_kit import get_logger
from mowdeck_service_kit.bus import (
    BusMessage,
    DropMessage,
    EventBus,
    MessageHandler,
    StreamStore,
    Subscription,
)
from pydantic import ValidationError

from identity.application.ports import EventLog
from identity.domain.erasure import event_names_account, event_names_user

_log = get_logger("identity.event_log")

USER_EVENTS_CONSUMER = "identity-user-events"
"""Durable consumer (and deliver group) of ``event.identity.user.deleted`` that removes the
deleted user's events from the stream."""


class BusEventLog:
    """:class:`~identity.application.ports.EventLog` on the bus's ``EVENTS`` stream.

    Without a bus, or with one that keeps no streams, there are no stored events: 0.
    """

    def __init__(self, bus: EventBus | None) -> None:
        self._store: StreamStore | None = bus if isinstance(bus, StreamStore) else None

    async def forget_account(self, account_id: UUID) -> int:
        """Erase every stored event of the account; how many."""
        return await self._erase(lambda event: event_names_account(event, account_id))

    async def forget_user(self, user_id: UUID) -> int:
        """Erase every stored event that names the user; how many."""
        return await self._erase(lambda event: event_names_user(event, user_id))

    async def _erase(self, names: Callable[[Mapping[str, Any]], bool]) -> int:
        store = self._store
        if store is None:
            return 0
        erased = 0
        async for message in store.stored_messages(event_wildcard()):
            try:
                event = json.loads(message.data)
            except ValueError:
                continue
            if isinstance(event, dict) and names(event) and await store.erase_message(message):
                erased += 1
        return erased


def user_deleted_handler(events: EventLog) -> MessageHandler:
    """Remove the events of a deleted user from the stream. Raises when the stream could not be
    read or changed (the event comes again); an event that cannot be decoded is dropped."""

    async def handle(message: BusMessage) -> None:
        try:
            event = message.model(EventEnvelope[UserChanged])
        except ValidationError as exc:
            raise DropMessage(
                f"invalid identity.user.deleted event: {exc.error_count()} errors"
            ) from exc
        if event.type != IDENTITY_USER_DELETED:
            return
        erased = await events.forget_user(event.payload.user_id)
        _log.info(
            "stored events of a deleted user erased",
            user_id=str(event.payload.user_id),
            events=erased,
        )

    return handle


async def subscribe_user_events(bus: EventBus, events: EventLog) -> Subscription:
    """Erase a user's stored events whenever a user is deleted."""
    return await bus.subscribe(
        event_subject_for_type(IDENTITY_USER_DELETED),
        user_deleted_handler(events),
        durable=USER_EVENTS_CONSUMER,
        queue=USER_EVENTS_CONSUMER,
    )
