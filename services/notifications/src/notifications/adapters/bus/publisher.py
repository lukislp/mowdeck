"""Publishes ``notifications.sent`` (``event.notifications.sent``, stream ``EVENTS``)."""

from mowdeck_contracts.v1 import NOTIFICATION_SENT, EventEnvelope, NotificationSent
from mowdeck_service_kit.bus import EventBus, publish_event


class BusPublisher:
    """Implements the ``EventPublisher`` port on the event bus."""

    def __init__(self, bus: EventBus, producer: str = "notifications") -> None:
        self._bus = bus
        self._producer = producer

    async def notification_sent(self, payload: NotificationSent) -> None:
        """Publish one ``notifications.sent`` event (msg id: the event id)."""
        await publish_event(
            self._bus,
            EventEnvelope[NotificationSent](
                type=NOTIFICATION_SENT,
                producer=self._producer,
                account_id=payload.account_id,
                payload=payload,
            ),
        )
