"""Subscriptions of realtime: one per subject family and instance.

* ``telemetry.mower.*`` and ``command.result.mower.*``: **without** durable and queue group,
  so the kit creates an ephemeral JetStream push consumer that delivers new messages only, and
  every instance receives every message (any instance can serve any client).
* ``device.mower.*.capabilities`` (ephemeral, ``last_per_subject``): the newest capabilities
  of every mower, so ``drive_start`` knows whether the mower offers manual driving.
* ``event.identity.grants.changed``: drop the cached decisions of the named subjects
  (``mowdeck_authz.events.apply_grants_changed``) and then re-check their sessions, in this
  order, so the re-check never reads a stale cached answer.

Handlers return quickly (the hub never awaits a client); malformed messages are dropped.
"""

from dataclasses import dataclass

from mowdeck_authz.events import GRANTS_CHANGED_SUBJECT, apply_grants_changed
from mowdeck_authz.writer import SupportsInvalidate
from mowdeck_contracts.v1 import (
    CommandResult,
    EventEnvelope,
    GrantsChanged,
    MowerCapabilitiesSnapshot,
    MowerState,
)
from mowdeck_contracts.v1.subjects import (
    ALL_COMMAND_RESULTS,
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

from realtime.application.hub import Hub


@dataclass
class RealtimeSubscriptions:
    """The active subscriptions; readiness needs ``telemetry`` and ``results``."""

    telemetry: Subscription
    results: Subscription
    grants: Subscription
    capabilities: Subscription

    def all(self) -> list[Subscription]:
        """Every subscription (for the shutdown)."""
        return [self.telemetry, self.results, self.grants, self.capabilities]


CAPABILITIES_SUBJECT = f"{DEVICE_PREFIX}.*.capabilities"
"""The capability snapshots of every mower."""


def _check_subject(message: BusMessage, mower_id: object) -> None:
    try:
        expected = mower_id_from_subject(message.subject)
    except ValueError as exc:
        raise DropMessage(f"unexpected subject {message.subject!r}") from exc
    if expected != mower_id:
        raise DropMessage("subject does not match the mower of the payload")


def state_handler(hub: Hub) -> MessageHandler:
    """Fan out a ``MowerState``."""

    async def handle(message: BusMessage) -> None:
        try:
            state = message.model(MowerState)
        except ValidationError as exc:
            hub.metrics.dropped("invalid")
            raise DropMessage(f"invalid mower state: {exc.error_count()} errors") from exc
        _check_subject(message, state.mower_id)
        hub.publish_state(state)

    return handle


def result_handler(hub: Hub) -> MessageHandler:
    """Fan out a ``CommandResult``."""

    async def handle(message: BusMessage) -> None:
        try:
            result = message.model(CommandResult)
        except ValidationError as exc:
            hub.metrics.dropped("invalid")
            raise DropMessage(f"invalid command result: {exc.error_count()} errors") from exc
        _check_subject(message, result.mower_id)
        hub.publish_result(result)

    return handle


def capabilities_handler(hub: Hub) -> MessageHandler:
    """Remember the newest capabilities of a mower (manual driving support)."""

    async def handle(message: BusMessage) -> None:
        try:
            mower_id, _ = parse_device_subject(message.subject)
            snapshot = message.model(MowerCapabilitiesSnapshot)
        except (ValueError, ValidationError) as exc:
            raise DropMessage("invalid capability snapshot") from exc
        if snapshot.mower_id != mower_id:
            raise DropMessage("subject does not match the snapshot's mower")
        hub.publish_capabilities(snapshot)

    return handle


def grants_handler(hub: Hub, cache: SupportsInvalidate | None) -> MessageHandler:
    """Invalidate the decision cache, then re-check the affected sessions."""

    async def handle(message: BusMessage) -> None:
        try:
            event = message.model(EventEnvelope[GrantsChanged])
        except ValidationError as exc:
            raise DropMessage(f"malformed grants.changed event: {exc.error_count()}") from exc
        change = event.payload
        if cache is not None:
            apply_grants_changed(cache, change)
        hub.grants_changed(
            change.subjects, all_subjects=change.all_subjects, account_id=change.account_id
        )

    return handle


async def subscribe_all(
    bus: EventBus, hub: Hub, cache: SupportsInvalidate | None
) -> RealtimeSubscriptions:
    """Subscribe the fan-out and the re-check trigger."""
    return RealtimeSubscriptions(
        telemetry=await bus.subscribe(ALL_TELEMETRY, state_handler(hub)),
        results=await bus.subscribe(ALL_COMMAND_RESULTS, result_handler(hub)),
        grants=await bus.subscribe(GRANTS_CHANGED_SUBJECT, grants_handler(hub, cache)),
        capabilities=await bus.subscribe(
            CAPABILITIES_SUBJECT, capabilities_handler(hub), last_per_subject=True
        ),
    )
