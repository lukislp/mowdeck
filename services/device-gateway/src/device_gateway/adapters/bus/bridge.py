"""Bridge scan requests (ADR 0038): ``gateway.mower.*.bridge.scan``, core NATS.

Fleet asks the gateway that holds the mower to look for it through a Bluetooth bridge
(``BridgeScanRequest`` in, ``BridgeScanReply`` out). Every gateway instance subscribes (no queue
group); only the instance whose shard holds the mower answers, the others stay silent, so Fleet
gets exactly one reply or a timeout. Each request is answered in its own task: a scan waits up
to ``timeout_s`` for the mower's advertisement and must not hold up the subscription. The
request carries the bridge's key: it is used, never logged. Answered requests are counted in
``mowdeck_device_gateway_bridge_scans_total{outcome}``.
"""

import asyncio

from mowdeck_contracts.v1 import BridgeScanRequest
from mowdeck_contracts.v1.subjects import ALL_BRIDGE_SCANS, parse_bridge_scan_subject
from mowdeck_service_kit import get_logger
from mowdeck_service_kit.bus import BusMessage, DropMessage, EventBus, Subscription
from prometheus_client import CollectorRegistry, Counter
from pydantic import ValidationError

from device_gateway.application.gateway import DeviceGateway

_log = get_logger(__name__)


class BridgeScanMetrics:
    """``mowdeck_device_gateway_bridge_scans_total{outcome}``."""

    def __init__(self, registry: CollectorRegistry | None) -> None:
        self._scans = Counter(
            "mowdeck_device_gateway_bridge_scans",
            "Answered bridge scan requests by outcome (ok or the error code).",
            ["outcome"],
            registry=registry,
        )

    def count(self, outcome: str) -> None:
        """Count one answered request."""
        self._scans.labels(outcome=outcome).inc()


class BridgeScanResponder:
    """Answers bridge scan requests for the mowers of this shard."""

    def __init__(self, gateway: DeviceGateway, metrics: BridgeScanMetrics | None = None) -> None:
        self._gateway = gateway
        self._metrics = metrics
        self._tasks: set[asyncio.Task[None]] = set()

    async def handle(self, message: BusMessage) -> None:
        """Decode and check the request, then answer it in the background."""
        try:
            request = message.model(BridgeScanRequest)
        except ValidationError as exc:
            # error_count only: the errors would echo the input, key included.
            raise DropMessage(f"invalid bridge scan request: {exc.error_count()} errors") from exc
        try:
            subject_mower = parse_bridge_scan_subject(message.subject)
        except ValueError as exc:
            raise DropMessage(f"unexpected subject {message.subject!r}") from exc
        if subject_mower != request.mower_id:
            raise DropMessage("subject does not match the request's mower")
        task = asyncio.create_task(self._answer(message, request))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _answer(self, message: BusMessage, request: BridgeScanRequest) -> None:
        reply = await self._gateway.bridge_scan(request)
        if reply is None:
            return  # another shard holds the mower
        try:
            await message.respond(reply)
        except Exception as exc:
            _log.warning(
                "bridge scan reply failed", mower_id=str(request.mower_id), error=repr(exc)
            )
            return
        outcome = "ok" if reply.error is None else reply.error.code.value
        if self._metrics is not None:
            self._metrics.count(outcome)


async def subscribe_bridge_scans(
    bus: EventBus, gateway: DeviceGateway, registry: CollectorRegistry | None = None
) -> Subscription:
    """Subscribe to ``gateway.mower.*.bridge.scan`` on every instance."""
    responder = BridgeScanResponder(gateway, BridgeScanMetrics(registry))
    return await bus.subscribe(ALL_BRIDGE_SCANS, responder.handle)
