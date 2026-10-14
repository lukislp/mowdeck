"""Publishes Identity's events on the bus (``event.identity.*``)."""

from typing import Any
from uuid import UUID

from mowdeck_contracts.v1 import EventEnvelope
from mowdeck_service_kit import get_logger
from mowdeck_service_kit.bus import EventBus, publish_event
from mowdeck_service_kit.correlation import get_correlation_id
from pydantic import BaseModel

_log = get_logger("identity.events")


class BusEventPublisher:
    """``EventPublisher`` port. Without a bus (NATS disabled) events are only logged."""

    def __init__(self, bus: EventBus | None, producer: str = "identity") -> None:
        self.bus = bus
        self.producer = producer

    async def publish(self, event_type: str, account_id: UUID | None, payload: BaseModel) -> None:
        """Publish; a failure is logged and swallowed (the change is already committed)."""
        if self.bus is None:
            _log.info("event not published (no bus)", event_type=event_type)
            return
        values: dict[str, object] = {
            "type": event_type,
            "producer": self.producer,
            "account_id": account_id,
            "payload": payload.model_dump(mode="json"),
        }
        correlation_id = get_correlation_id()
        if correlation_id:
            values["correlation_id"] = correlation_id
        envelope = EventEnvelope[dict[str, Any]].model_validate(values)
        try:
            await publish_event(self.bus, envelope)
        except Exception:
            _log.exception("publishing an event failed", event_type=event_type)
