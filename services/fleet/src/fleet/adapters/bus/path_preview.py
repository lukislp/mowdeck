"""Asks the Device-Gateway to plan a route without starting (core NATS request/reply)."""

from mowdeck_contracts.v1 import PathPreviewGatewayRequest, PathPreviewReply
from mowdeck_contracts.v1.subjects import path_preview_subject
from mowdeck_service_kit.bus import BusNotConnectedError, EventBus


class BusPathPreviewGateway:
    """``PathPreviewGateway`` port over ``gateway.mower.<id>.path.preview``."""

    def __init__(self, bus: EventBus | None, *, timeout_seconds: float) -> None:
        self.bus = bus
        self.timeout_seconds = timeout_seconds

    async def request(self, request: PathPreviewGatewayRequest) -> PathPreviewReply:
        """Send the request; raises the bus errors (no bus, no responders, timeout)."""
        if self.bus is None or not self.bus.is_connected:
            msg = "the event bus is not connected"
            raise BusNotConnectedError(msg)
        reply = await self.bus.request(
            path_preview_subject(request.mower_id), request, self.timeout_seconds
        )
        return reply.model(PathPreviewReply)
