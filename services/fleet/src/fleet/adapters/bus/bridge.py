"""Asks the Device-Gateway to look for a mower through a Bluetooth bridge (core NATS
request/reply, ADR 0038). Fleet never speaks to a bridge itself: the gateway that holds the
mower connects to the proxy and answers."""

from mowdeck_contracts.v1 import BridgeScanReply, BridgeScanRequest
from mowdeck_contracts.v1.subjects import bridge_scan_subject
from mowdeck_service_kit.bus import BusNotConnectedError, EventBus


class BusBridgeScanGateway:
    """``BridgeScanGateway`` port over ``gateway.mower.<id>.bridge.scan``."""

    def __init__(self, bus: EventBus | None, *, timeout_seconds: float) -> None:
        self.bus = bus
        self.timeout_seconds = timeout_seconds

    async def request(self, request: BridgeScanRequest) -> BridgeScanReply:
        """Send the request; raises the bus errors (no bus, no responders, timeout)."""
        if self.bus is None or not self.bus.is_connected:
            msg = "the event bus is not connected"
            raise BusNotConnectedError(msg)
        reply = await self.bus.request(
            bridge_scan_subject(request.mower_id), request, self.timeout_seconds
        )
        return reply.model(BridgeScanReply)
