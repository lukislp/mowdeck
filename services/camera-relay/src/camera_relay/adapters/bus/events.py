"""Publishes the relay events on ``camera.mower.<id>.relay.<kind>`` (stream ``CAMERA``)."""

from collections.abc import Callable
from typing import Any

from mowdeck_contracts.v1 import EventEnvelope
from mowdeck_service_kit import get_logger
from mowdeck_service_kit.bus import EventBus
from mowdeck_service_kit.bus.base import EVENT_TYPE_HEADER, EVENT_VERSION_HEADER
from mowdeck_service_kit.correlation import CORRELATION_HEADER

_log = get_logger("camera_relay.events")


class BusEventPublisher:
    """The ``EventPublisher`` port over the event bus (``msg_id`` = event id)."""

    def __init__(self, bus: Callable[[], EventBus | None]) -> None:
        self._bus = bus

    async def publish(self, subject: str, envelope: EventEnvelope[Any]) -> None:
        """Publish ``envelope``; without a bus (tests, NATS disabled) the event is dropped."""
        bus = self._bus()
        if bus is None:
            _log.debug("no bus: relay event not published", type=envelope.type)
            return
        await bus.publish(
            subject,
            envelope,
            msg_id=str(envelope.id),
            headers={
                CORRELATION_HEADER: envelope.correlation_id,
                EVENT_TYPE_HEADER: envelope.type,
                EVENT_VERSION_HEADER: str(envelope.version),
            },
        )
