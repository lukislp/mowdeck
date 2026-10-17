"""Asks the Device-Gateway for a live camera session (core NATS request/reply, ADR 0032) and
the camera relay for leases (``relay.camera.*``, ADR 0045)."""

from mowdeck_contracts.v1 import CameraSessionReply, CameraSessionRequest
from mowdeck_contracts.v1.relay import (
    RelayAttachReply,
    RelayAttachRequest,
    RelayDetachReply,
    RelayDetachRequest,
    RelayRenewReply,
    RelayRenewRequest,
)
from mowdeck_contracts.v1.subjects import (
    RELAY_CAMERA_ATTACH,
    RELAY_CAMERA_DETACH,
    RELAY_CAMERA_RENEW,
    camera_session_subject,
)
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


class BusCameraRelay:
    """``CameraRelay`` port over ``relay.camera.attach``, ``renew`` and ``detach``."""

    def __init__(self, bus: EventBus | None, *, timeout_seconds: float) -> None:
        self.bus = bus
        self.timeout_seconds = timeout_seconds

    def _bus(self) -> EventBus:
        if self.bus is None or not self.bus.is_connected:
            msg = "the event bus is not connected"
            raise BusNotConnectedError(msg)
        return self.bus

    async def attach(self, request: RelayAttachRequest) -> RelayAttachReply:
        """Open a lease; raises the bus errors (no relay deployed: ``NoRespondersError``)."""
        reply = await self._bus().request(RELAY_CAMERA_ATTACH, request, self.timeout_seconds)
        return reply.model(RelayAttachReply)

    async def renew(self, request: RelayRenewRequest) -> RelayRenewReply:
        """The heartbeat of a lease (a fresh ticket)."""
        reply = await self._bus().request(RELAY_CAMERA_RENEW, request, self.timeout_seconds)
        return reply.model(RelayRenewReply)

    async def detach(self, request: RelayDetachRequest) -> RelayDetachReply:
        """End a lease (idempotent)."""
        reply = await self._bus().request(RELAY_CAMERA_DETACH, request, self.timeout_seconds)
        return reply.model(RelayDetachReply)
