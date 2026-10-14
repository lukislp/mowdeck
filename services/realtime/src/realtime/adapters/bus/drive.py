"""Manual driving over the bus: Fleet's session requests and the frames (ADR 0026).

* ``fleet.drive.start`` / ``fleet.drive.stop``: core NATS requests to Fleet (queue ``fleet``).
* Frames: :func:`~mowdeck_service_kit.bus.publish_command` on ``command.mower.<id>`` (stream
  ``COMMANDS``), consumed by the Device-Gateway shard that holds the mower.
"""

from collections.abc import Callable

from mowdeck_contracts.v1 import (
    CommandEnvelope,
    DriveStartReply,
    DriveStartRequest,
    DriveStopRequest,
)
from mowdeck_contracts.v1.subjects import FLEET_DRIVE_START, FLEET_DRIVE_STOP
from mowdeck_service_kit import get_logger
from mowdeck_service_kit.bus import BusError, EventBus, publish_command
from pydantic import ValidationError

from realtime.domain.errors import DriveUnavailableError

_log = get_logger("realtime.drive")


class BusDriveBackend:
    """The :class:`~realtime.application.ports.DriveBackend` on the service's bus."""

    def __init__(self, bus: Callable[[], EventBus | None], *, timeout: float) -> None:
        self._bus = bus
        self._timeout = timeout

    def _active(self) -> EventBus:
        bus = self._bus()
        if bus is None or not bus.is_connected:
            msg = "the event bus is not connected"
            raise DriveUnavailableError(msg)
        return bus

    async def start(self, request: DriveStartRequest) -> DriveStartReply:
        """Ask Fleet to open a session."""
        bus = self._active()
        try:
            reply = await bus.request(FLEET_DRIVE_START, request, timeout=self._timeout)
            return reply.model(DriveStartReply)
        except (BusError, ValidationError) as exc:
            _log.warning("fleet.drive.start failed", error=repr(exc))
            msg = "fleet did not answer"
            raise DriveUnavailableError(msg) from exc

    async def stop(self, request: DriveStopRequest) -> None:
        """Ask Fleet to close the session record; failures are logged only."""
        try:
            bus = self._active()
            await bus.request(FLEET_DRIVE_STOP, request, timeout=self._timeout)
        except (BusError, DriveUnavailableError) as exc:
            _log.warning(
                "fleet.drive.stop failed, Fleet expires the session",
                session_id=str(request.session_id),
                error=repr(exc),
            )

    async def publish(self, envelope: CommandEnvelope) -> None:
        """Publish one frame (``msg id = command_id``)."""
        await publish_command(self._active(), envelope)
