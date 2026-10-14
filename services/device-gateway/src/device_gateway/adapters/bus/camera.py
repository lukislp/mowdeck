"""Camera session requests (ADR 0032): ``gateway.mower.*.camera.session``, core NATS.

Every gateway instance subscribes (no queue group); only the instance whose shard holds the
mower answers, the others stay silent, so Fleet gets exactly one reply or a timeout. Counted per
outcome in ``mowdeck_device_gateway_camera_sessions_total``. The reply carries a short-lived
token: it is sent, never logged.
"""

from mowdeck_contracts.v1 import CameraSessionRequest
from mowdeck_contracts.v1.subjects import ALL_CAMERA_SESSIONS, parse_camera_session_subject
from mowdeck_service_kit import get_logger
from mowdeck_service_kit.bus import BusMessage, DropMessage, EventBus, Subscription
from prometheus_client import CollectorRegistry, Counter
from pydantic import ValidationError

from device_gateway.application.gateway import DeviceGateway

_log = get_logger(__name__)


class CameraMetrics:
    """``mowdeck_device_gateway_camera_sessions_total{outcome}``."""

    def __init__(self, registry: CollectorRegistry | None) -> None:
        self._sessions = Counter(
            "mowdeck_device_gateway_camera_sessions",
            "Answered camera session requests by outcome (issued or the error code).",
            ["outcome"],
            registry=registry,
        )

    def count(self, outcome: str) -> None:
        """Count one answered request."""
        self._sessions.labels(outcome=outcome).inc()


class CameraResponder:
    """Answers camera session requests for the mowers of this shard."""

    def __init__(self, gateway: DeviceGateway, metrics: CameraMetrics | None = None) -> None:
        self._gateway = gateway
        self._metrics = metrics

    async def handle(self, message: BusMessage) -> None:
        """Decode, ask the gateway and reply (not at all for mowers of other shards)."""
        try:
            request = message.model(CameraSessionRequest)
        except ValidationError as exc:
            raise DropMessage(f"invalid camera request: {exc.error_count()} errors") from exc
        try:
            subject_mower = parse_camera_session_subject(message.subject)
        except ValueError as exc:
            raise DropMessage(f"unexpected subject {message.subject!r}") from exc
        if subject_mower != request.mower_id:
            raise DropMessage("subject does not match the request's mower")
        reply = await self._gateway.camera_session(request)
        if reply is None:
            return  # another shard holds the mower
        await message.respond(reply)
        outcome = "issued" if reply.error is None else reply.error.code.value
        if self._metrics is not None:
            self._metrics.count(outcome)
        _log.info(
            "camera session answered",
            mower_id=str(request.mower_id),
            subject=str(request.requested_by.subject),
            outcome=outcome,
            provider=reply.session.provider.value if reply.session else None,
        )


async def subscribe_camera_requests(
    bus: EventBus, gateway: DeviceGateway, registry: CollectorRegistry | None = None
) -> Subscription:
    """Subscribe to ``gateway.mower.*.camera.session`` on every instance."""
    responder = CameraResponder(gateway, CameraMetrics(registry))
    return await bus.subscribe(ALL_CAMERA_SESSIONS, responder.handle)
