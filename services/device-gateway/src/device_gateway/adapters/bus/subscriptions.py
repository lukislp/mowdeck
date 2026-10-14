"""Subscriptions of device-gateway: commands of all mowers, the Fleet registry events and the
photo requests of Vision (``gateway.mower.*.photo``, core NATS, every instance; only the instance
that holds the mower answers).

Handler rules: return to acknowledge, raise to get a redelivery, raise ``DropMessage`` to
discard a poison message.
"""

from typing import Any

from mowdeck_contracts.v1 import (
    MOWER_REGISTERED,
    MOWER_REMOVED,
    MOWER_UPDATED,
    CommandEnvelope,
    EventEnvelope,
    MowerRegistration,
    PhotoRequest,
)
from mowdeck_contracts.v1.subjects import (
    ALL_COMMANDS,
    ALL_PHOTO_REQUESTS,
    event_wildcard,
    mower_id_from_subject,
    parse_photo_subject,
)
from mowdeck_service_kit import get_logger
from mowdeck_service_kit.bus import BusMessage, DropMessage, EventBus, Subscription
from pydantic import ValidationError

from device_gateway.application.gateway import DeviceGateway

_log = get_logger(__name__)

_REGISTRY_EVENTS = frozenset({MOWER_REGISTERED, MOWER_UPDATED, MOWER_REMOVED})


class CommandSubscription:
    """The command consumer of this shard; ``active`` backs the readiness check."""

    def __init__(self, bus: EventBus, gateway: DeviceGateway, *, durable: str) -> None:
        self._bus = bus
        self._gateway = gateway
        self._durable = durable
        self._subscription: Subscription | None = None

    @property
    def active(self) -> bool:
        """True while the consumer is subscribed and the bus is connected."""
        return self._subscription is not None and self._bus.is_connected

    async def start(self) -> None:
        """Subscribe to ``command.mower.*`` with the shard's durable consumer."""
        if self._subscription is None:
            self._subscription = await self._bus.subscribe(
                ALL_COMMANDS, self.handle, durable=self._durable
            )

    async def stop(self) -> None:
        """Stop receiving commands; the durable consumer keeps its position on the server."""
        subscription, self._subscription = self._subscription, None
        if subscription is not None:
            await subscription.unsubscribe()

    async def handle(self, message: BusMessage) -> None:
        """Decode and admit one command. Execution continues in the mower's own task."""
        try:
            envelope = message.model(CommandEnvelope)
        except ValidationError as exc:
            raise DropMessage(f"invalid command envelope: {exc.error_count()} errors") from exc
        try:
            subject_mower = mower_id_from_subject(message.subject)
        except ValueError as exc:
            raise DropMessage(f"unexpected subject {message.subject!r}") from exc
        if subject_mower != envelope.mower_id:
            _log.warning(
                "command subject and envelope disagree",
                subject=message.subject,
                command_id=str(envelope.command_id),
            )
            raise DropMessage("subject does not match the envelope's mower")
        await self._gateway.submit(envelope)


async def subscribe_registry_events(bus: EventBus, gateway: DeviceGateway) -> Subscription:
    """Follow ``mower.registered``, ``mower.updated`` and ``mower.removed`` from Fleet.

    Every instance needs every event, so the consumer is neither durable nor shared; the
    start-up request to Fleet covers what happened while the instance was down.
    """

    async def handle(message: BusMessage) -> None:
        try:
            event = message.model(EventEnvelope[dict[str, Any]])
        except ValidationError as exc:
            raise DropMessage("invalid event envelope") from exc
        if event.type not in _REGISTRY_EVENTS:
            return  # e.g. our own mower.connected
        try:
            registration = MowerRegistration.model_validate(event.payload)
        except ValidationError as exc:
            raise DropMessage(f"invalid {event.type} payload") from exc
        if event.type == MOWER_REMOVED:
            await gateway.remove(registration.mower_id, source=event.type)
        else:
            await gateway.register(registration, source=event.type)

    return await bus.subscribe(event_wildcard("mower"), handle)


async def subscribe_photo_requests(
    bus: EventBus, gateway: DeviceGateway, *, timeout_seconds: float = 5.0
) -> Subscription:
    """Answer ``gateway.mower.<id>.photo`` for the mowers this instance holds.

    No queue group: every instance receives every request and only the one that holds the mower
    responds, so the first (and only) reply is the right one. Requests for other mowers stay
    unanswered here.
    """

    async def handle(message: BusMessage) -> None:
        try:
            request = message.model(PhotoRequest)
            subject_mower = parse_photo_subject(message.subject)
        except (ValidationError, ValueError) as exc:
            raise DropMessage("invalid photo request") from exc
        if subject_mower != request.mower_id:
            raise DropMessage("subject does not match the request's mower")
        reply = await gateway.photo(request, timeout_seconds=timeout_seconds)
        if reply is not None:
            await message.respond(reply)

    return await bus.subscribe(ALL_PHOTO_REQUESTS, handle)
