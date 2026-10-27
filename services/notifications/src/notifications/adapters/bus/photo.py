"""Takes the camera photo of an error or theft event from the Device-Gateway (ADR 0035, 0045).

Core NATS request/reply on ``gateway.mower.<id>.photo``, the subject Vision and Fleet use: the
gateway answers with the mower's own photo function or a frame of the camera relay's tap (an
open viewer session is reused, no new stream is started for it). The demand is
``error_analysis``: the mower has a problem or is being carried off, so the relay may wake it
and the Vision budget does not apply; the notifications keep their own budget (fan-out).
"""

import base64
import binascii
from uuid import UUID

from mowdeck_contracts.v1 import PhotoReply, PhotoRequest, VisionDemand
from mowdeck_contracts.v1.subjects import photo_subject
from mowdeck_service_kit import get_logger
from mowdeck_service_kit.bus import BusError, EventBus
from pydantic import ValidationError

from notifications.application.ports import CapturedPhoto

_log = get_logger("notifications.photo")


class BusPhotoSource:
    """``PhotoSource`` over ``gateway.mower.<id>.photo``."""

    def __init__(self, bus: EventBus, *, timeout_seconds: float, max_bytes: int) -> None:
        self._bus = bus
        self._timeout = timeout_seconds
        self._max_bytes = max_bytes

    async def capture(self, mower_id: UUID, account_id: UUID) -> CapturedPhoto | None:
        """One image (never logged), None when the gateway has none."""
        if not self._bus.is_connected:
            return None
        try:
            message = await self._bus.request(
                photo_subject(mower_id),
                PhotoRequest(
                    mower_id=mower_id,
                    account_id=account_id,
                    max_bytes=self._max_bytes,
                    demand=VisionDemand.ERROR_ANALYSIS,
                    max_frames=1,
                ),
                timeout=self._timeout,
            )
            reply = message.model(PhotoReply)
        except (BusError, ValidationError) as exc:
            _log.info("no photo from the gateway", mower_id=str(mower_id), error=type(exc).__name__)
            return None
        if reply.error is not None:
            _log.info(
                "the gateway delivered no photo",
                mower_id=str(mower_id),
                code=reply.error.code.value,
            )
            return None
        if reply.frames:
            frame = reply.frames[0]
            encoded, media_type = frame.image_base64, frame.media_type
        elif reply.image_base64 is not None and reply.media_type is not None:
            encoded, media_type = reply.image_base64, reply.media_type
        else:
            return None
        try:
            data = base64.b64decode(encoded, validate=True)
        except (binascii.Error, ValueError):
            return None
        if not data or len(data) > self._max_bytes:
            return None
        return CapturedPhoto(data, media_type)
