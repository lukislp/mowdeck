"""Firmware check requests (ADR 0038): ``gateway.mower.*.firmware.check``, core NATS.

Fleet relays a user's "check for updates" to the gateway that holds the mower
(``FirmwareCheckRequest`` in, ``FirmwareCheckReply`` out). Every gateway instance subscribes (no
queue group); only the instance whose shard holds the mower answers, the others stay silent.
The gateway asks the manufacturer through the connection's firmware port and publishes the
state that carries the answer, so clients see it on the telemetry stream as well. Answered
requests are counted in ``mowdeck_device_gateway_firmware_checks_total{outcome}``.
"""

from mowdeck_contracts.v1 import FirmwareCheckRequest
from mowdeck_contracts.v1.subjects import ALL_FIRMWARE_CHECKS, parse_firmware_check_subject
from mowdeck_service_kit import get_logger
from mowdeck_service_kit.bus import BusMessage, DropMessage, EventBus, Subscription
from prometheus_client import CollectorRegistry, Counter
from pydantic import ValidationError

from device_gateway.application.gateway import DeviceGateway

_log = get_logger(__name__)


class FirmwareCheckMetrics:
    """``mowdeck_device_gateway_firmware_checks_total{outcome}``."""

    def __init__(self, registry: CollectorRegistry | None) -> None:
        self._checks = Counter(
            "mowdeck_device_gateway_firmware_checks",
            "Answered firmware check requests by outcome (info or the error code).",
            ["outcome"],
            registry=registry,
        )

    def count(self, outcome: str) -> None:
        """Count one answered request."""
        self._checks.labels(outcome=outcome).inc()


class FirmwareCheckResponder:
    """Answers firmware check requests for the mowers of this shard."""

    def __init__(self, gateway: DeviceGateway, metrics: FirmwareCheckMetrics | None = None) -> None:
        self._gateway = gateway
        self._metrics = metrics

    async def handle(self, message: BusMessage) -> None:
        """Decode, ask the gateway and reply (not at all for mowers of other shards)."""
        try:
            request = message.model(FirmwareCheckRequest)
        except ValidationError as exc:
            raise DropMessage(
                f"invalid firmware check request: {exc.error_count()} errors"
            ) from exc
        try:
            subject_mower = parse_firmware_check_subject(message.subject)
        except ValueError as exc:
            raise DropMessage(f"unexpected subject {message.subject!r}") from exc
        if subject_mower != request.mower_id:
            raise DropMessage("subject does not match the request's mower")
        reply = await self._gateway.firmware_check(request)
        if reply is None:
            return  # another shard holds the mower
        await message.respond(reply)
        outcome = "info" if reply.error is None else reply.error.code.value
        if self._metrics is not None:
            self._metrics.count(outcome)
        _log.info("firmware check answered", mower_id=str(request.mower_id), outcome=outcome)


async def subscribe_firmware_checks(
    bus: EventBus, gateway: DeviceGateway, registry: CollectorRegistry | None = None
) -> Subscription:
    """Subscribe to ``gateway.mower.*.firmware.check`` on every instance."""
    responder = FirmwareCheckResponder(gateway, FirmwareCheckMetrics(registry))
    return await bus.subscribe(ALL_FIRMWARE_CHECKS, responder.handle)
