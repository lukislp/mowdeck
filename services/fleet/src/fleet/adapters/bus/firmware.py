"""Asks the Device-Gateway for the firmware state of a mower (core NATS request/reply, ADR 0038).
The gateway that holds the mower asks the vendor through the mower's connection and answers."""

from mowdeck_contracts.v1 import FirmwareCheckReply, FirmwareCheckRequest
from mowdeck_contracts.v1.subjects import firmware_check_subject
from mowdeck_service_kit.bus import BusNotConnectedError, EventBus


class BusFirmwareCheckGateway:
    """``FirmwareCheckGateway`` port over ``gateway.mower.<id>.firmware.check``."""

    def __init__(self, bus: EventBus | None, *, timeout_seconds: float) -> None:
        self.bus = bus
        self.timeout_seconds = timeout_seconds

    async def request(self, request: FirmwareCheckRequest) -> FirmwareCheckReply:
        """Send the request; raises the bus errors (no bus, no responders, timeout)."""
        if self.bus is None or not self.bus.is_connected:
            msg = "the event bus is not connected"
            raise BusNotConnectedError(msg)
        reply = await self.bus.request(
            firmware_check_subject(request.mower_id), request, self.timeout_seconds
        )
        return reply.model(FirmwareCheckReply)
