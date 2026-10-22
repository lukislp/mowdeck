"""Publishes ``notifications.sent`` and ``notifications.raised`` (``event.notifications.*``,
stream ``EVENTS``)."""

from mowdeck_contracts.v1 import (
    NOTIFICATION_RAISED,
    NOTIFICATION_SENT,
    EventEnvelope,
    NotificationRaised,
    NotificationSent,
)
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

    async def notification_raised(self, payload: NotificationRaised) -> None:
        """Publish one ``notifications.raised`` event. The event id is the notification id
        (stable per occurrence), so JetStream's duplicate window drops a repeated publish."""
        await publish_event(
            self._bus,
            EventEnvelope[NotificationRaised](
                id=payload.notification_id,
                type=NOTIFICATION_RAISED,
                producer=self._producer,
                account_id=payload.account_id,
                occurred_at=payload.occurred_at,
                payload=payload,
            ),
        )
