"""The ``TrackSource`` port over core NATS request/reply (``tracks.positions.query``)."""

from mowdeck_contracts.v1 import PositionTrackReply, PositionTrackRequest
from mowdeck_contracts.v1.subjects import TELEMETRY_POSITIONS_QUERY
from mowdeck_service_kit import get_logger
from mowdeck_service_kit.bus import EventBus
from pydantic import ValidationError

from planner.domain.errors import Unavailable

_log = get_logger("planner.telemetry")


class BusTrackSource:
    """Asks Telemetry; every failure (no bus, no responder, timeout, garbage) is
    :class:`Unavailable`."""

    def __init__(self, bus: EventBus | None, timeout_seconds: float = 5.0) -> None:
        self.bus = bus
        self.timeout_seconds = timeout_seconds

    async def track(self, request: PositionTrackRequest) -> PositionTrackReply:
        """``tracks.positions.query``."""
        if self.bus is None or not self.bus.is_connected:
            raise Unavailable("telemetry is not reachable: the event bus is not connected")
        try:
            message = await self.bus.request(
                TELEMETRY_POSITIONS_QUERY, request, self.timeout_seconds
            )
        except Exception as exc:
            _log.warning("telemetry did not answer", error=repr(exc))
            raise Unavailable("telemetry did not answer the track query") from exc
        try:
            return message.model(PositionTrackReply)
        except ValidationError as exc:
            raise Unavailable("telemetry answered with an invalid track") from exc
