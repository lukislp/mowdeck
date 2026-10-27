"""Tracking lease requests (ADR 0053): ``gateway.mower.*.tracking``, core NATS.

Telemetry renews a lease every few seconds while a theft tracking session is active
(``TrackingLeaseRequest`` in, ``TrackingLeaseReply`` out). Every gateway instance subscribes (no
queue group); only the instance whose shard holds the mower answers. Answered requests are counted
in ``mowdeck_device_gateway_tracking_leases_total{outcome}``.
"""

from mowdeck_contracts.v1 import TrackingLeaseRequest
from mowdeck_contracts.v1.subjects import ALL_TRACKING_LEASES, parse_tracking_lease_subject
from mowdeck_service_kit.bus import BusMessage, DropMessage, EventBus, Subscription
from prometheus_client import CollectorRegistry, Counter
from pydantic import ValidationError

from device_gateway.application.gateway import DeviceGateway


class TrackingMetrics:
    """``mowdeck_device_gateway_tracking_leases_total{outcome}``."""

    def __init__(self, registry: CollectorRegistry | None) -> None:
        self._leases = Counter(
            "mowdeck_device_gateway_tracking_leases",
            "Answered tracking lease requests by outcome (the reply code).",
            ["outcome"],
            registry=registry,
        )

    def count(self, outcome: str) -> None:
        """Count one answered request."""
        self._leases.labels(outcome=outcome).inc()


class TrackingLeaseResponder:
    """Answers tracking lease requests for the mowers of this shard."""

    def __init__(self, gateway: DeviceGateway, metrics: TrackingMetrics | None = None) -> None:
        self._gateway = gateway
        self._metrics = metrics

    async def handle(self, message: BusMessage) -> None:
        """Decode, ask the gateway and reply (not at all for mowers of other shards)."""
        try:
            request = message.model(TrackingLeaseRequest)
        except ValidationError as exc:
            raise DropMessage(
                f"invalid tracking lease request: {exc.error_count()} errors"
            ) from exc
        try:
            subject_mower = parse_tracking_lease_subject(message.subject)
        except ValueError as exc:
            raise DropMessage(f"unexpected subject {message.subject!r}") from exc
        if subject_mower != request.mower_id:
            raise DropMessage("subject does not match the request's mower")
        reply = await self._gateway.tracking_lease(request)
        if reply is None:
            return  # another shard holds the mower
        await message.respond(reply)
        if self._metrics is not None:
            self._metrics.count(reply.code.value)


async def subscribe_tracking_leases(
    bus: EventBus, gateway: DeviceGateway, registry: CollectorRegistry | None = None
) -> Subscription:
    """Subscribe to ``gateway.mower.*.tracking`` on every instance."""
    responder = TrackingLeaseResponder(gateway, TrackingMetrics(registry))
    return await bus.subscribe(ALL_TRACKING_LEASES, responder.handle)
