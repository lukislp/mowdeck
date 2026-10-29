"""Asks the Device-Gateway for the devices of a vendor cloud account and for the enabled cloud
providers (core NATS request/reply, ADR 0034 and ADR 0065). Fleet never imports an adapter
package: the gateway logs in through the provider plugins and answers."""

from mowdeck_contracts.v1 import (
    CloudDiscoverReply,
    CloudDiscoverRequest,
    CloudProvidersReply,
    CloudProvidersRequest,
)
from mowdeck_contracts.v1.subjects import GATEWAY_CLOUD_DISCOVER, GATEWAY_CLOUD_PROVIDERS
from mowdeck_service_kit.bus import BusNotConnectedError, EventBus


class BusCloudDiscovery:
    """``CloudDiscovery`` port over ``gateway.cloud.discover`` and ``gateway.cloud.providers``."""

    def __init__(self, bus: EventBus | None, *, timeout_seconds: float) -> None:
        self.bus = bus
        self.timeout_seconds = timeout_seconds

    def _connected(self) -> EventBus:
        if self.bus is None or not self.bus.is_connected:
            msg = "the event bus is not connected"
            raise BusNotConnectedError(msg)
        return self.bus

    async def discover(self, request: CloudDiscoverRequest) -> CloudDiscoverReply:
        """Send the request; raises the bus errors (no bus, no responders, timeout)."""
        reply = await self._connected().request(
            GATEWAY_CLOUD_DISCOVER, request, self.timeout_seconds
        )
        return reply.model(CloudDiscoverReply)

    async def providers(self) -> CloudProvidersReply:
        """Ask for the enabled providers; raises the bus errors."""
        reply = await self._connected().request(
            GATEWAY_CLOUD_PROVIDERS, CloudProvidersRequest(), self.timeout_seconds
        )
        return reply.model(CloudProvidersReply)
