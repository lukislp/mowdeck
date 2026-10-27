"""Bus adapters of the theft tracking mode (ADR 0053).

* :class:`BusTrackingEvents` publishes ``tracking.*`` events (stream ``EVENTS``).
* :class:`BusTrackingGateway` asks the Device-Gateway for a lease (``gateway.mower.<id>.tracking``)
  and for photos (``gateway.mower.<id>.photo``), core NATS request/reply.
* :func:`notification_handler` starts a session on a theft alarm: Notifications publishes every
  derived notification as ``notifications.raised`` (``event.notifications.raised``).
"""

from collections.abc import Callable

from mowdeck_contracts.v1 import (
    NOTIFICATION_RAISED,
    EventEnvelope,
    NotificationRaised,
    PhotoReply,
    PhotoRequest,
    TrackingEvent,
    TrackingLeaseReply,
    TrackingLeaseRequest,
)
from mowdeck_contracts.v1.subjects import (
    event_wildcard,
    photo_subject,
    tracking_lease_subject,
)
from mowdeck_service_kit import get_logger
from mowdeck_service_kit.bus import (
    BusMessage,
    DropMessage,
    EventBus,
    MessageHandler,
    Subscription,
    publish_event,
)
from pydantic import ValidationError

from telemetry.application.tracking import Tracking
from telemetry.domain.errors import UnknownTrackedMowerError
from telemetry.domain.tracking import THEFT_ALARM_KINDS

_log = get_logger("telemetry.tracking.bus")

NOTIFICATIONS_CONSUMER = "telemetry-notifications"
"""Durable consumer (and queue group) of ``notifications.raised``."""


class BusTrackingEvents:
    """Implements :class:`~telemetry.application.tracking_ports.TrackingEvents`."""

    def __init__(self, bus: Callable[[], EventBus | None]) -> None:
        self._bus = bus

    async def publish(self, event_type: str, event: TrackingEvent) -> None:
        """Publish one tracking event (no-op without a bus)."""
        bus = self._bus()
        if bus is None:
            return
        await publish_event(
            bus,
            EventEnvelope[TrackingEvent](
                type=event_type,
                producer="telemetry",
                account_id=event.account_id,
                occurred_at=event.at,
                payload=event,
            ),
        )


class BusTrackingGateway:
    """Implements :class:`~telemetry.application.tracking_ports.TrackingGateway`."""

    def __init__(
        self,
        bus: Callable[[], EventBus | None],
        *,
        lease_timeout_seconds: float = 3.0,
        photo_timeout_seconds: float = 30.0,
    ) -> None:
        self._bus = bus
        self._lease_timeout = lease_timeout_seconds
        self._photo_timeout = photo_timeout_seconds

    async def lease(self, request: TrackingLeaseRequest) -> TrackingLeaseReply | None:
        """Take, renew or release a lease; None without a bus."""
        bus = self._bus()
        if bus is None:
            return None
        reply = await bus.request(
            tracking_lease_subject(request.mower_id), request, self._lease_timeout
        )
        return reply.model(TrackingLeaseReply)

    async def photo(self, request: PhotoRequest) -> PhotoReply | None:
        """Ask for a still image; None without a bus."""
        bus = self._bus()
        if bus is None:
            return None
        reply = await bus.request(photo_subject(request.mower_id), request, self._photo_timeout)
        return reply.model(PhotoReply)


def notification_handler(tracking: Tracking) -> MessageHandler:
    """Start tracking on a theft alarm notification."""

    async def handle(message: BusMessage) -> None:
        try:
            event = message.model(EventEnvelope[dict[str, object]])
        except ValidationError as exc:
            raise DropMessage("invalid event envelope") from exc
        if event.type != NOTIFICATION_RAISED:
            return
        try:
            raised = NotificationRaised.model_validate(event.payload)
        except ValidationError as exc:
            raise DropMessage(f"invalid {event.type} payload") from exc
        if (
            raised.kind.value not in THEFT_ALARM_KINDS
            or raised.mower_id is None
            or not tracking.config.auto_start
        ):
            return
        try:
            _, created = await tracking.start(raised.account_id, raised.mower_id, raised.kind.value)
        except UnknownTrackedMowerError as exc:
            raise DropMessage(exc.message) from exc
        if created:
            _log.info(
                "tracking started by alarm", mower_id=str(raised.mower_id), kind=raised.kind.value
            )

    return handle


async def subscribe_notifications(bus: EventBus, tracking: Tracking) -> Subscription:
    """Subscribe to ``event.notifications.>`` for the theft alarms."""
    return await bus.subscribe(
        event_wildcard("notifications"),
        notification_handler(tracking),
        durable=NOTIFICATIONS_CONSUMER,
        queue=NOTIFICATIONS_CONSUMER,
    )
