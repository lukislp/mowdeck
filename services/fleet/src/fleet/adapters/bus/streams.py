# SPDX-FileCopyrightText: 2026 Lukas Körber
# SPDX-License-Identifier: AGPL-3.0-only

"""What the message streams hold of a mower, and its removal (ADR 0082).

Four streams store messages on subjects that carry the mower id: the commands and their results
(``COMMANDS``), the states (``TELEMETRY``), the last map, capabilities and mow path (``DEVICE``)
and the camera relay events (``CAMERA``). They hold positions, the garden's map and the name of
whoever sent a command, for up to 30 days. Fleet owns the mowers, so Fleet removes a mower's
messages, by subject filter, when the mower goes:

* :class:`BusMowerStreams` is the :class:`~fleet.application.ports.MowerStreams` port over the
  kit's :class:`~mowdeck_service_kit.bus.StreamStore`. The five filters are written out one by
  one so the repository's subject scan sees each (the NATS permissions, ``nats_auth.py``).
* :func:`subscribe_mower_streams` purges on ``event.mower.removed`` (durable
  ``fleet-mower-streams``): every removal, whoever caused it (the API, a linked account that
  went, a demo sandbox, the account erasure). A purge that fails is delivered again.

The account erasure purges as well, in its own request, because it must not confirm before the
messages are gone (:mod:`fleet.application.erasure`).
"""

from uuid import UUID

from mowdeck_contracts.v1 import MOWER_REMOVED, EventEnvelope, MowerRegistration
from mowdeck_contracts.v1.subjects import (
    camera_wildcard,
    command_result_subject,
    command_subject,
    device_wildcard,
    event_subject_for_type,
    telemetry_subject,
)
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

from fleet.application.ports import MowerStreams

_log = get_logger("fleet.streams")

MOWER_STREAMS_CONSUMER = "fleet-mower-streams"
"""Durable consumer (and deliver group) of ``event.mower.removed`` that purges the streams."""


class BusMowerStreams:
    """:class:`~fleet.application.ports.MowerStreams` on the bus's streams.

    Without a bus, or with one that keeps no streams, there is nothing to purge: 0.
    """

    def __init__(self, bus: EventBus | None) -> None:
        self._store: StreamStore | None = bus if isinstance(bus, StreamStore) else None

    async def purge(self, mower_id: UUID) -> int:
        """Remove every stored message of the mower; how many."""
        store = self._store
        if store is None:
            return 0
        purged = await store.purge_subject(command_subject(mower_id))
        purged += await store.purge_subject(command_result_subject(mower_id))
        purged += await store.purge_subject(telemetry_subject(mower_id))
        purged += await store.purge_subject(device_wildcard(mower_id))
        purged += await store.purge_subject(camera_wildcard(mower_id))
        return purged

    async def remaining(self, mower_id: UUID) -> int:
        """How many messages of the mower the streams still hold."""
        store = self._store
        if store is None:
            return 0
        left = await store.count_subject(command_subject(mower_id))
        left += await store.count_subject(command_result_subject(mower_id))
        left += await store.count_subject(telemetry_subject(mower_id))
        left += await store.count_subject(device_wildcard(mower_id))
        left += await store.count_subject(camera_wildcard(mower_id))
        return left


def mower_removed_handler(streams: MowerStreams) -> MessageHandler:
    """Purge the streams of a removed mower. Raises when the purge failed (the event comes
    again); an event that cannot be decoded is dropped."""

    async def handle(message: BusMessage) -> None:
        try:
            event = message.model(EventEnvelope[MowerRegistration])
        except ValidationError as exc:
            raise DropMessage(f"invalid mower.removed event: {exc.error_count()} errors") from exc
        if event.type != MOWER_REMOVED:
            return
        mower_id = event.payload.mower_id
        purged = await streams.purge(mower_id)
        _log.info("streams of a removed mower purged", mower_id=str(mower_id), messages=purged)

    return handle


async def subscribe_mower_streams(bus: EventBus, streams: MowerStreams) -> Subscription:
    """Purge a mower's stream messages whenever a mower is removed."""
    return await bus.subscribe(
        event_subject_for_type(MOWER_REMOVED),
        mower_removed_handler(streams),
        durable=MOWER_STREAMS_CONSUMER,
        queue=MOWER_STREAMS_CONSUMER,
    )
