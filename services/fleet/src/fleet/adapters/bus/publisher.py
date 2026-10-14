"""Publishes Fleet's events (``event.mower.*``) and the commands (``command.mower.<id>``)."""

from uuid import UUID

from mowdeck_contracts.v1 import CommandEnvelope, EventEnvelope
from mowdeck_service_kit import get_logger
from mowdeck_service_kit.bus import BusNotConnectedError, EventBus, publish_command, publish_event
from mowdeck_service_kit.correlation import get_correlation_id
from pydantic import BaseModel

_log = get_logger("fleet.events")


class BusEventPublisher:
    """``EventPublisher`` port. Without a bus (NATS disabled) events are only logged."""

    def __init__(self, bus: EventBus | None, producer: str = "fleet") -> None:
        self.bus = bus
        self.producer = producer

    async def publish(self, event_type: str, account_id: UUID, payload: BaseModel) -> None:
        """Publish; a failure is logged and swallowed (the change is already committed)."""
        if self.bus is None:
            _log.info("event not published (no bus)", event_type=event_type)
            return
        values: dict[str, object] = {
            "type": event_type,
            "producer": self.producer,
            "account_id": account_id,
            "payload": payload,
        }
        correlation_id = get_correlation_id()
        if correlation_id:
            values["correlation_id"] = correlation_id
        try:
            envelope = EventEnvelope[type(payload)].model_validate(values)  # type: ignore[misc]
            await publish_event(self.bus, envelope)
        except Exception:
            _log.exception("publishing an event failed", event_type=event_type)


class BusCommandPublisher:
    """``CommandPublisher`` port over JetStream (``msg id = command_id``)."""

    def __init__(self, bus: EventBus | None) -> None:
        self.bus = bus

    async def publish(self, envelope: CommandEnvelope) -> None:
        """Publish or raise (no bus, disconnected, expired, server error)."""
        if self.bus is None or not self.bus.is_connected:
            msg = "the event bus is not connected"
            raise BusNotConnectedError(msg)
        await publish_command(self.bus, envelope)
