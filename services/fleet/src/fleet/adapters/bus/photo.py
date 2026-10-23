"""Asks the Device-Gateway for a still image of a mower's camera (core NATS request/reply on
``gateway.mower.<id>.photo``, the subject Vision uses; ADR 0045). The gateway takes it with the
mower's own photo function or through the camera relay's frame tap."""

from mowdeck_contracts.v1 import PhotoReply, PhotoRequest
from mowdeck_contracts.v1.subjects import photo_subject
from mowdeck_service_kit.bus import BusNotConnectedError, EventBus


class BusPhotoGateway:
    """``PhotoGateway`` port over ``gateway.mower.<id>.photo``."""

    def __init__(self, bus: EventBus | None, *, timeout_seconds: float) -> None:
        self.bus = bus
        self.timeout_seconds = timeout_seconds

    async def request(self, request: PhotoRequest) -> PhotoReply:
        """Send the request; raises the bus errors (no bus, no responders, timeout)."""
        if self.bus is None or not self.bus.is_connected:
            msg = "the event bus is not connected"
            raise BusNotConnectedError(msg)
        reply = await self.bus.request(
            photo_subject(request.mower_id), request, self.timeout_seconds
        )
        return reply.model(PhotoReply)
