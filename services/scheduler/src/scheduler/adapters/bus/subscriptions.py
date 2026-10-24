"""Subscriptions of the Scheduler.

* ``telemetry.mower.*`` (JetStream ``TELEMETRY``, durable and queue group ``scheduler-states``):
  the latest activity and position of every mower (busy check, last known location).
* ``device.mower.>`` (JetStream ``DEVICE``, durable and queue group ``scheduler-device``): the
  map snapshots (geo origin for the weather, area ids for "all areas"); capability snapshots
  are acknowledged and ignored here.
* ``device.mower.*.capabilities`` (JetStream ``DEVICE``, durable and queue group
  ``scheduler-capabilities``, newest message per subject first): the capabilities of every
  mower, to check a schedule's start settings and to fit each run to its mower. A consumer of
  its own, so the existing ``scheduler-device`` consumer keeps its configuration and a new
  installation learns every mower's capabilities at once.
* ``event.identity.grants.changed`` (every instance): drops cached authorization decisions, so
  a revoked ``can_control`` stops the next firing at once.

Handler rules: return to acknowledge, raise to get a redelivery, raise ``DropMessage`` to
discard a poison message. Malformed payloads are dropped, never redelivered.
"""

from mowdeck_authz.events import subscribe_cache_invalidation
from mowdeck_authz.writer import SupportsInvalidate
from mowdeck_contracts.v1 import MowerCapabilitiesSnapshot, MowerMapSnapshot, MowerState
from mowdeck_contracts.v1.subjects import (
    ALL_DEVICE_SNAPSHOTS,
    ALL_TELEMETRY,
    DEVICE_PREFIX,
    mower_id_from_subject,
    parse_device_subject,
)
from mowdeck_service_kit.bus import (
    BusMessage,
    DropMessage,
    EventBus,
    MessageHandler,
    Subscription,
)
from pydantic import ValidationError

from scheduler.application.ingest import Ingest

STATES_CONSUMER = "scheduler-states"
"""Durable consumer (and queue group) of the state stream, shared by all replicas."""
DEVICE_CONSUMER = "scheduler-device"
"""Durable consumer (and queue group) of the device snapshots."""
CAPABILITIES_CONSUMER = "scheduler-capabilities"
"""Durable consumer (and queue group) of the capability snapshots."""
ALL_CAPABILITY_SNAPSHOTS = f"{DEVICE_PREFIX}.*.capabilities"
"""The capability snapshots of every mower."""


def state_handler(ingest: Ingest) -> MessageHandler:
    """Remember the latest activity of a mower."""

    async def handle(message: BusMessage) -> None:
        try:
            state = message.model(MowerState)
        except ValidationError as exc:
            raise DropMessage(f"invalid mower state: {exc.error_count()} errors") from exc
        try:
            subject_mower = mower_id_from_subject(message.subject)
        except ValueError as exc:
            raise DropMessage(f"unexpected subject {message.subject!r}") from exc
        if subject_mower != state.mower_id:
            raise DropMessage("subject does not match the mower of the state")
        await ingest.on_state(state)

    return handle


def snapshot_handler(ingest: Ingest) -> MessageHandler:
    """Remember the map facts of a mower."""

    async def handle(message: BusMessage) -> None:
        try:
            mower_id, kind = parse_device_subject(message.subject)
        except ValueError as exc:
            raise DropMessage(f"unexpected subject {message.subject!r}") from exc
        if kind != "map":
            return
        try:
            snapshot = message.model(MowerMapSnapshot)
        except ValidationError as exc:
            raise DropMessage(f"invalid map snapshot: {exc.error_count()} errors") from exc
        if snapshot.mower_id != mower_id:
            raise DropMessage("subject does not match the mower of the snapshot")
        await ingest.on_map(snapshot)

    return handle


def capabilities_handler(ingest: Ingest) -> MessageHandler:
    """Remember the newest capabilities of a mower."""

    async def handle(message: BusMessage) -> None:
        try:
            mower_id, kind = parse_device_subject(message.subject)
        except ValueError as exc:
            raise DropMessage(f"unexpected subject {message.subject!r}") from exc
        if kind != "capabilities":
            return
        try:
            snapshot = message.model(MowerCapabilitiesSnapshot)
        except ValidationError as exc:
            raise DropMessage(f"invalid capabilities snapshot: {exc.error_count()} errors") from exc
        if snapshot.mower_id != mower_id:
            raise DropMessage("subject does not match the mower of the snapshot")
        await ingest.on_capabilities(snapshot)

    return handle


async def subscribe_all(
    bus: EventBus, ingest: Ingest, cache: SupportsInvalidate | None
) -> list[Subscription]:
    """Subscribe every handler of the service and return the subscriptions."""
    subscriptions: list[Subscription] = [
        await bus.subscribe(
            ALL_TELEMETRY, state_handler(ingest), durable=STATES_CONSUMER, queue=STATES_CONSUMER
        ),
        await bus.subscribe(
            ALL_DEVICE_SNAPSHOTS,
            snapshot_handler(ingest),
            durable=DEVICE_CONSUMER,
            queue=DEVICE_CONSUMER,
        ),
        await bus.subscribe(
            ALL_CAPABILITY_SNAPSHOTS,
            capabilities_handler(ingest),
            durable=CAPABILITIES_CONSUMER,
            queue=CAPABILITIES_CONSUMER,
            last_per_subject=True,
        ),
    ]
    if cache is not None:
        subscriptions.append(await subscribe_cache_invalidation(bus, cache))
    return subscriptions
