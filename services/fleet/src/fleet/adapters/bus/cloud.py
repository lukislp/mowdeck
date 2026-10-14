"""Asks the Device-Gateway for the devices of a vendor cloud account (core NATS request/reply,
ADR 0034). Fleet never imports the GPL-3.0 adapter: the gateway logs in and answers."""

from mowdeck_contracts.v1 import CloudDiscoverReply, CloudDiscoverRequest
from mowdeck_contracts.v1.subjects import GATEWAY_CLOUD_DISCOVER
from mowdeck_service_kit.bus import BusNotConnectedError, EventBus


class BusCloudDiscovery:
    """``CloudDiscovery`` port over ``gateway.cloud.discover``."""

    def __init__(self, bus: EventBus | None, *, timeout_seconds: float) -> None:
        self.bus = bus
        self.timeout_seconds = timeout_seconds

    async def discover(self, request: CloudDiscoverRequest) -> CloudDiscoverReply:
        """Send the request; raises the bus errors (no bus, no responders, timeout)."""
        if self.bus is None or not self.bus.is_connected:
            msg = "the event bus is not connected"
            raise BusNotConnectedError(msg)
        reply = await self.bus.request(GATEWAY_CLOUD_DISCOVER, request, self.timeout_seconds)
        return reply.model(CloudDiscoverReply)
