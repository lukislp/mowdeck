"""Asks the Device-Gateway for a live camera session (core NATS request/reply, ADR 0032)."""

from mowdeck_contracts.v1 import CameraSessionReply, CameraSessionRequest
from mowdeck_contracts.v1.subjects import camera_session_subject
from mowdeck_service_kit.bus import BusNotConnectedError, EventBus


class BusCameraGateway:
    """``CameraGateway`` port over ``gateway.mower.<id>.camera.session``."""

    def __init__(self, bus: EventBus | None, *, timeout_seconds: float) -> None:
        self.bus = bus
        self.timeout_seconds = timeout_seconds

    async def request(self, request: CameraSessionRequest) -> CameraSessionReply:
        """Send the request; raises the bus errors (no bus, no responders, timeout)."""
        if self.bus is None or not self.bus.is_connected:
            msg = "the event bus is not connected"
            raise BusNotConnectedError(msg)
        reply = await self.bus.request(
            camera_session_subject(request.mower_id), request, self.timeout_seconds
        )
        return reply.model(CameraSessionReply)
