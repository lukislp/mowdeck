"""Cloud account discovery (ADR 0034): ``gateway.cloud.discover``, core NATS request/reply.

Any gateway instance may answer (queue group ``device-gateway``): the discovery is not tied to
a mower. Counted per outcome in ``mowdeck_device_gateway_cloud_discoveries_total``. The request
carries the password of the vendor account: it is used, never logged.
"""

from mowdeck_contracts.v1 import CloudDiscoverRequest
from mowdeck_contracts.v1.subjects import GATEWAY_CLOUD_DISCOVER
from mowdeck_service_kit.bus import BusMessage, DropMessage, EventBus, Subscription
from prometheus_client import CollectorRegistry, Counter
from pydantic import ValidationError

from device_gateway.application.discovery import CloudDiscoveryService

QUEUE = "device-gateway"


class DiscoveryMetrics:
    """``mowdeck_device_gateway_cloud_discoveries_total{outcome}``."""

    def __init__(self, registry: CollectorRegistry | None) -> None:
        self._discoveries = Counter(
            "mowdeck_device_gateway_cloud_discoveries",
            "Answered cloud discovery requests by outcome (listed or the error code).",
            ["outcome"],
            registry=registry,
        )

    def count(self, outcome: str) -> None:
        """Count one answered request."""
        self._discoveries.labels(outcome=outcome).inc()


class DiscoveryResponder:
    """Decodes requests and answers them through :class:`CloudDiscoveryService`."""

    def __init__(self, service: CloudDiscoveryService) -> None:
        self._service = service

    async def handle(self, message: BusMessage) -> None:
        """Decode, discover and reply."""
        try:
            request = message.model(CloudDiscoverRequest)
        except ValidationError as exc:
            # error_count only: the errors would echo the input, password included.
            raise DropMessage(f"invalid discovery request: {exc.error_count()} errors") from exc
        await message.respond(await self._service.discover(request))


async def subscribe_cloud_discovery(bus: EventBus, service: CloudDiscoveryService) -> Subscription:
    """Subscribe to ``gateway.cloud.discover`` in the queue group ``device-gateway``."""
    responder = DiscoveryResponder(service)
    return await bus.subscribe(GATEWAY_CLOUD_DISCOVER, responder.handle, queue=QUEUE)
