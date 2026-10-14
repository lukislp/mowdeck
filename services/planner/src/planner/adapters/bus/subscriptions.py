"""Subscriptions of the Planner.

* ``device.mower.>`` (JetStream ``DEVICE``, durable and queue group ``planner-device``, newest
  message per subject first): the map and capability snapshots of every mower, so the Planner
  knows areas, zones, the geo reference and whether a mower follows custom paths.
* ``event.identity.grants.changed`` (every instance): drops cached authorization decisions.

Handler rules: return to acknowledge, raise to get a redelivery, raise ``DropMessage`` to
discard a poison message. Malformed payloads are dropped, never redelivered.
"""

from mowdeck_authz.events import subscribe_cache_invalidation
from mowdeck_authz.writer import SupportsInvalidate
from mowdeck_contracts.v1 import MowerCapabilitiesSnapshot, MowerMapSnapshot
from mowdeck_contracts.v1.subjects import ALL_DEVICE_SNAPSHOTS, parse_device_subject
from mowdeck_service_kit.bus import (
    BusMessage,
    DropMessage,
    EventBus,
    MessageHandler,
    Subscription,
)
from pydantic import ValidationError

from planner.application.ingest import Ingest

DEVICE_CONSUMER = "planner-device"
"""Durable consumer (and queue group) of the device snapshots."""


def snapshot_handler(ingest: Ingest) -> MessageHandler:
    """Remember the newest map or capabilities of a mower."""

    async def handle(message: BusMessage) -> None:
        try:
            mower_id, kind = parse_device_subject(message.subject)
        except ValueError as exc:
            raise DropMessage(f"unexpected subject {message.subject!r}") from exc
        if kind not in ("map", "capabilities"):
            return  # the mow path and later kinds are not the planner's
        snapshot: MowerMapSnapshot | MowerCapabilitiesSnapshot
        try:
            if kind == "map":
                snapshot = message.model(MowerMapSnapshot)
            else:
                snapshot = message.model(MowerCapabilitiesSnapshot)
        except ValidationError as exc:
            raise DropMessage(f"invalid {kind} snapshot: {exc.error_count()} errors") from exc
        if snapshot.mower_id != mower_id:
            raise DropMessage("subject does not match the mower of the snapshot")
        if isinstance(snapshot, MowerMapSnapshot):
            await ingest.on_map(snapshot)
        else:
            await ingest.on_capabilities(snapshot)

    return handle


async def subscribe_all(
    bus: EventBus, ingest: Ingest, cache: SupportsInvalidate | None
) -> list[Subscription]:
    """Subscribe every handler of the service and return the subscriptions."""
    subscriptions: list[Subscription] = [
        await bus.subscribe(
            ALL_DEVICE_SNAPSHOTS,
            snapshot_handler(ingest),
            durable=DEVICE_CONSUMER,
            queue=DEVICE_CONSUMER,
            last_per_subject=True,
        ),
    ]
    if cache is not None:
        subscriptions.append(await subscribe_cache_invalidation(bus, cache))
    return subscriptions
