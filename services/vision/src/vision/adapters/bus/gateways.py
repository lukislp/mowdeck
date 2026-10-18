"""Requests to other services and publications over NATS.

* :class:`BusFleetGateway`: ``fleet.drive.start``, ``fleet.drive.stop``, ``fleet.commands.issue``.
* :class:`BusPhotoSource`: ``gateway.mower.<id>.photo`` (the Device-Gateway holding the mower),
  asking for both front cameras of the same moment (``max_frames`` 2).
* :class:`BusPublisher`: drive frames on ``command.mower.<id>`` and
  ``vision.proposal.created`` on ``event.vision.proposal.created``.
"""

import base64
import binascii
from uuid import UUID

from mowdeck_contracts.v1 import (
    VISION_FRAME_SET_MAX_FRAMES,
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
    VisionDemand,
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

    async def photos(self, mower_id: UUID, account_id: UUID) -> list[Image]:
        """The images of one moment from the gateway (logged without content): both front
        cameras, left first, when it takes them through the camera relay, else its single
        image; empty when none can be had.

        Every photo Vision asks for helps a mower with a problem (AI assist, the automatic
        analysis of a recoverable error), so it is an ``error_analysis``: through the camera
        relay it may wake a sleeping mower and is not bound by the Vision budget (ADR 0045).
        A gateway of an older release ignores ``max_frames`` and answers its single image."""
        if self.bus is None:
            return []
        try:
            message = await self.bus.request(
                photo_subject(mower_id),
                PhotoRequest(
                    mower_id=mower_id,
                    account_id=account_id,
                    demand=VisionDemand.ERROR_ANALYSIS,
                    max_frames=VISION_FRAME_SET_MAX_FRAMES,
                ),
                timeout=self.timeout_seconds,
            )
            reply = message.model(PhotoReply)
        except (BusError, ValidationError) as exc:
            _log.info("no photo from the gateway", mower_id=str(mower_id), error=type(exc).__name__)
            return []
        if reply.error is not None:
            _log.info(
                "the gateway delivered no photo",
                mower_id=str(mower_id),
                code=reply.error.code.value,
            )
            return []
        images = _images(reply)
        _log.info(
            "photo received",
            mower_id=str(mower_id),
            images=len(images),
            sides=[image.side for image in images],
            missing=reply.frames_missing,
            bytes=sum(len(image.data) for image in images),
        )
        return images


def _decode(data: str) -> bytes | None:
    try:
        return base64.b64decode(data, validate=True)
    except (binascii.Error, ValueError):
        return None


def _images(reply: PhotoReply) -> list[Image]:
    """The images of a photo reply (source ``mower``); undecodable ones are skipped."""
    if reply.frames:
        images: list[Image] = []
        for frame in reply.frames:
            data = _decode(frame.image_base64)
            if data:
                images.append(
                    Image(
                        data,
                        frame.media_type,
                        "mower",
                        camera=frame.camera_name or frame.camera_id,
                        side=frame.side,
                        captured_at=frame.captured_at,
                    )
                )
        return images
    if reply.image_base64 is None or reply.media_type is None:
        return []
    data = _decode(reply.image_base64)
    return [Image(data, reply.media_type, "mower")] if data else []


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
