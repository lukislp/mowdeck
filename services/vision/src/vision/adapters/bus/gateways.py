"""Requests to other services and publications over NATS.

* :class:`BusFleetGateway`: ``fleet.drive.start``, ``fleet.drive.stop``, ``fleet.commands.issue``.
* :class:`BusPhotoSource`: ``gateway.mower.<id>.photo`` (the Device-Gateway holding the mower).
* :class:`BusPublisher`: drive frames on ``command.mower.<id>`` and
  ``vision.proposal.created`` on ``event.vision.proposal.created``.
"""

import base64
import binascii
from uuid import UUID

from mowdeck_contracts.v1 import (
    VISION_PROPOSAL_CREATED,
    CommandEnvelope,
    DriveStartReply,
    DriveStartRequest,
    DriveStopReply,
    DriveStopRequest,
    EventEnvelope,
    IssueCommandReply,
    IssueCommandRequest,
    PhotoReply,
    PhotoRequest,
    VisionProposalCreated,
)
from mowdeck_contracts.v1.subjects import (
    FLEET_COMMANDS_ISSUE,
    FLEET_DRIVE_START,
    FLEET_DRIVE_STOP,
    photo_subject,
)
from mowdeck_service_kit import get_logger
from mowdeck_service_kit.bus import BusError, EventBus, publish_command, publish_event
from mowdeck_service_kit.correlation import get_correlation_id
from pydantic import ValidationError

from vision.domain.models import Image

_log = get_logger("vision.bus")


class NoBus(RuntimeError):  # noqa: N818 - reads as a state
    """NATS is disabled: nothing can be driven or requested."""


class BusFleetGateway:
    """``FleetGateway`` over core NATS requests."""

    def __init__(self, bus: EventBus | None, *, timeout_seconds: float = 5.0) -> None:
        self.bus = bus
        self.timeout_seconds = timeout_seconds

    def _bus(self) -> EventBus:
        if self.bus is None:
            raise NoBus("no event bus")
        return self.bus

    async def start_drive(self, request: DriveStartRequest) -> DriveStartReply:
        """``fleet.drive.start`` (raises when Fleet does not answer)."""
        reply = await self._bus().request(FLEET_DRIVE_START, request, timeout=self.timeout_seconds)
        return reply.model(DriveStartReply)

    async def stop_drive(self, request: DriveStopRequest) -> DriveStopReply | None:
        """``fleet.drive.stop``; None (logged) when Fleet does not answer."""
        try:
            reply = await self._bus().request(
                FLEET_DRIVE_STOP, request, timeout=self.timeout_seconds
            )
            return reply.model(DriveStopReply)
        except (BusError, NoBus, ValidationError) as exc:
            _log.warning(
                "fleet.drive.stop failed", session_id=str(request.session_id), error=repr(exc)
            )
            return None

    async def issue(self, request: IssueCommandRequest) -> IssueCommandReply:
        """``fleet.commands.issue`` (raises when Fleet does not answer)."""
        reply = await self._bus().request(
            FLEET_COMMANDS_ISSUE, request, timeout=self.timeout_seconds
        )
        return reply.model(IssueCommandReply)


class BusPhotoSource:
    """``PhotoSource`` over ``gateway.mower.<id>.photo``."""

    def __init__(self, bus: EventBus | None, *, timeout_seconds: float = 6.0) -> None:
        self.bus = bus
        self.timeout_seconds = timeout_seconds

    async def photo(self, mower_id: UUID, account_id: UUID) -> Image | None:
        """An image from the gateway, None when none can be had (logged without content)."""
        if self.bus is None:
            return None
        try:
            message = await self.bus.request(
                photo_subject(mower_id),
                PhotoRequest(mower_id=mower_id, account_id=account_id),
                timeout=self.timeout_seconds,
            )
            reply = message.model(PhotoReply)
        except (BusError, ValidationError) as exc:
            _log.info("no photo from the gateway", mower_id=str(mower_id), error=type(exc).__name__)
            return None
        if reply.error is not None or reply.image_base64 is None or reply.media_type is None:
            code = reply.error.code.value if reply.error else "empty"
            _log.info("the gateway delivered no photo", mower_id=str(mower_id), code=code)
            return None
        try:
            data = base64.b64decode(reply.image_base64, validate=True)
        except (binascii.Error, ValueError):
            return None
        _log.info("photo received", mower_id=str(mower_id), bytes=len(data))
        return Image(data, reply.media_type, "mower")


class BusPublisher:
    """``FramePublisher`` and ``EventPublisher``."""

    def __init__(self, bus: EventBus | None, *, producer: str = "vision") -> None:
        self.bus = bus
        self.producer = producer

    async def frame(self, envelope: CommandEnvelope) -> None:
        """Publish one drive frame (raises when the bus does not take it)."""
        if self.bus is None:
            raise NoBus("no event bus: frames cannot be published")
        await publish_command(self.bus, envelope)

    async def proposal_created(self, event: VisionProposalCreated) -> None:
        """Publish ``vision.proposal.created``; failures are logged."""
        if self.bus is None:
            _log.info("event not published (no bus)", event_type=VISION_PROPOSAL_CREATED)
            return
        values: dict[str, object] = {
            "type": VISION_PROPOSAL_CREATED,
            "producer": self.producer,
            "account_id": event.account_id,
            "payload": event,
        }
        correlation_id = get_correlation_id()
        if correlation_id:
            values["correlation_id"] = correlation_id
        try:
            await publish_event(
                self.bus, EventEnvelope[VisionProposalCreated].model_validate(values)
            )
        except Exception:
            _log.exception("publishing an event failed", event_type=VISION_PROPOSAL_CREATED)
