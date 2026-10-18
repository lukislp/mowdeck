"""The frame tap's internal HTTP routes (Vision through the relay, ADR 0045; never routed by
Traefik, the relay has no route at all).

* ``POST /internal/v1/frames/snapshot`` with a :class:`SnapshotBody` answers one ``image/jpeg``
  with ``X-Frame-Captured-At`` and ``X-Frame-Received-At`` (ISO 8601, the relay's wall clock;
  the capture time is the best estimate, the receive time where nothing better is known),
  ``X-Frame-Camera``, ``X-Frame-Width`` and ``X-Frame-Height``.
* ``GET /internal/v1/frames/stream?account_id&mower_id&camera_id&fps&max_width`` answers
  ``multipart/x-mixed-replace; boundary=frame``: one JPEG part per frame with
  ``Content-Length``, ``X-Frame-Index`` and the two time headers. The lease lives while the
  connection is open and the client pulls (at most 300 s).

Both need ``Authorization: Bearer <MOWDECK_RELAY_FRAMES_TOKEN>``; without a configured token the
tap is off (``503 frames-disabled``). Errors are problem documents:

======  =============================  ===================================================
Status  Problem type                   When
======  =============================  ===================================================
401     ``unauthorized``               missing or wrong token
404     ``not-found``                  the mower is not the account's (``forbidden``,
                                       ``not_found``)
409     ``camera-budget``              a Vision limit: battery, video budget, daily minutes
503     ``camera-unavailable``         offline or asleep, the source failed, relay full
503     ``camera-relay-unsupported``   no source driver for the mower's camera provider
503     ``frames-disabled``            no token configured
503     ``frames-busy``                too many frame requests at once
504     ``frame-timeout``              no frame in time
502     ``frame-failed``               the camera path gave no usable frame
======  =============================  ===================================================

Images and tokens are never logged.
"""

import asyncio
import hmac
from collections.abc import AsyncIterator, Callable
from datetime import datetime
from typing import Annotated, ClassVar, Final
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Query, Response
from fastapi.responses import StreamingResponse
from mowdeck_contracts.v1.principal import PrincipalKind, PrincipalRef
from mowdeck_contracts.v1.relay import RelayErrorCode
from mowdeck_contracts.v1.vision import PHOTO_MAX_BYTES
from mowdeck_service_kit import get_logger
from mowdeck_service_kit.errors import NotFound, ServiceError, Unauthorized
from pydantic import BaseModel, ConfigDict, Field, model_validator
from starlette.background import BackgroundTask

from camera_relay.application.frames import (
    FAILED,
    SNAPSHOT_MAX_SECONDS,
    STREAM_MAX_FPS,
    TIMEOUT,
    Frame,
    FrameRequest,
    FrameStream,
    FrameTap,
    FrameTapError,
)

_log = get_logger("camera_relay.http.frames")

BOUNDARY: Final = "frame"
FRAMES_SERVICE_SUBJECT: Final = UUID("8f3c2b61-0d4e-4a7b-9c55-2e1f6a9b7d10")
"""Subject of the lease's principal when the caller names no service principal."""
BUSY_WAIT_SECONDS: Final = 5.0
"""How long a request waits for a free slot before ``frames-busy``."""


class FramesDisabled(ServiceError):  # noqa: N818 - reads as a state
    """No token configured: the frame tap is off."""

    status: ClassVar[int] = 503
    problem_type: ClassVar[str] = "frames-disabled"
    title: ClassVar[str] = "The frame tap is off"


class FramesBusy(ServiceError):  # noqa: N818 - reads as a state
    """Too many frame requests at once."""

    status: ClassVar[int] = 503
    problem_type: ClassVar[str] = "frames-busy"
    title: ClassVar[str] = "Too many frame requests"


class CameraBudget(ServiceError):  # noqa: N818 - reads as a state
    """A Vision limit refused the lease."""

    status: ClassVar[int] = 409
    problem_type: ClassVar[str] = "camera-budget"
    title: ClassVar[str] = "A camera limit refused the frames"


class CameraUnavailable(ServiceError):  # noqa: N818 - reads as a state
    """The camera cannot deliver now."""

    status: ClassVar[int] = 503
    problem_type: ClassVar[str] = "camera-unavailable"
    title: ClassVar[str] = "The camera is not available"


class RelayUnsupported(ServiceError):  # noqa: N818 - reads as a state
    """No source driver for the mower's camera provider."""

    status: ClassVar[int] = 503
    problem_type: ClassVar[str] = "camera-relay-unsupported"
    title: ClassVar[str] = "The relay cannot serve this camera"


class FrameTimeout(ServiceError):  # noqa: N818 - reads as an outcome
    """No frame in time."""

    status: ClassVar[int] = 504
    problem_type: ClassVar[str] = "frame-timeout"
    title: ClassVar[str] = "No camera frame in time"


class FrameFailed(ServiceError):  # noqa: N818 - reads as an outcome
    """No usable frame."""

    status: ClassVar[int] = 502
    problem_type: ClassVar[str] = "frame-failed"
    title: ClassVar[str] = "No usable camera frame"


_ERRORS: Final[dict[str, type[ServiceError]]] = {
    RelayErrorCode.BUDGET.value: CameraBudget,
    RelayErrorCode.UNAVAILABLE.value: CameraUnavailable,
    RelayErrorCode.UNSUPPORTED_PROVIDER.value: RelayUnsupported,
    RelayErrorCode.FORBIDDEN.value: NotFound,
    RelayErrorCode.NOT_FOUND.value: NotFound,
    TIMEOUT: FrameTimeout,
    FAILED: FrameFailed,
}


def _problem(error: FrameTapError) -> ServiceError:
    if error.code in (RelayErrorCode.FORBIDDEN.value, RelayErrorCode.NOT_FOUND.value):
        return NotFound("mower not found")
    return _ERRORS.get(error.code, FrameFailed)(error.detail[:500])


class SnapshotBody(BaseModel):
    """``POST /internal/v1/frames/snapshot``."""

    model_config = ConfigDict(extra="forbid")

    account_id: UUID
    mower_id: UUID
    camera_id: str | None = Field(default=None, min_length=1, max_length=64)
    """None: the first camera with a publisher uid (the front or left one)."""
    max_width: int = Field(default=1280, ge=160, le=3840)
    max_bytes: int = Field(default=PHOTO_MAX_BYTES, ge=1024, le=8 * 1024 * 1024)
    timeout_s: float = Field(default=20.0, gt=0, le=SNAPSHOT_MAX_SECONDS)
    requested_by: PrincipalRef | None = None
    """The requesting service (kind ``service``); it owns the lease."""
    on_behalf_of: UUID | None = None
    """The user the service acts for (logged only)."""

    @model_validator(mode="after")
    def _service_principal(self) -> "SnapshotBody":
        if self.requested_by is not None and self.requested_by.kind is not PrincipalKind.SERVICE:
            msg = "requested_by must be a service principal"
            raise ValueError(msg)
        return self


def _principal(given: PrincipalRef | None) -> PrincipalRef:
    return given or PrincipalRef(
        subject=FRAMES_SERVICE_SUBJECT, kind=PrincipalKind.SERVICE, display_name="frame-tap"
    )


def _iso(at: datetime) -> str:
    return at.isoformat(timespec="milliseconds")


def _frame_headers(frame: Frame) -> dict[str, str]:
    headers = {
        "X-Frame-Captured-At": _iso(frame.captured_at),
        "X-Frame-Received-At": _iso(frame.received_at),
        "Cache-Control": "no-store",
    }
    if frame.width is not None and frame.height is not None:
        headers["X-Frame-Width"] = str(frame.width)
        headers["X-Frame-Height"] = str(frame.height)
    return headers


def _part(frame: Frame, index: int) -> bytes:
    head = [
        f"--{BOUNDARY}",
        "Content-Type: image/jpeg",
        f"Content-Length: {len(frame.data)}",
        f"X-Frame-Index: {index}",
        *(f"{name}: {value}" for name, value in _frame_headers(frame).items()),
    ]
    return ("\r\n".join(head) + "\r\n\r\n").encode("ascii") + frame.data + b"\r\n"


def frames_router(
    tap: Callable[[], FrameTap | None],
    token: Callable[[], str | None],
    *,
    max_concurrency: int = 4,
) -> APIRouter:
    """The frame tap's routes. ``tap`` is None without a media plane (``camera-unavailable``)."""
    router = APIRouter()
    slots = asyncio.Semaphore(max_concurrency)

    def authorized(authorization: Annotated[str | None, Header()] = None) -> None:
        expected = token()
        if not expected:
            raise FramesDisabled("set MOWDECK_RELAY_FRAMES_TOKEN to serve frames")
        scheme, _, given = (authorization or "").partition(" ")
        if scheme.lower() != "bearer" or not hmac.compare_digest(
            given.strip().encode(), expected.encode()
        ):
            raise Unauthorized("a valid frame tap token is required")

    def active_tap() -> FrameTap:
        current = tap()
        if current is None:
            raise CameraUnavailable("the relay has no media plane")
        return current

    async def acquire() -> None:
        try:
            async with asyncio.timeout(BUSY_WAIT_SECONDS):
                await slots.acquire()
        except TimeoutError as exc:
            raise FramesBusy("too many frame requests; retry shortly") from exc

    @router.post(
        "/internal/v1/frames/snapshot",
        include_in_schema=False,
        dependencies=[Depends(authorized)],
    )
    async def snapshot(body: SnapshotBody) -> Response:
        current = active_tap()
        await acquire()
        try:
            frame = await current.snapshot(
                FrameRequest(
                    account_id=body.account_id,
                    mower_id=body.mower_id,
                    requested_by=_principal(body.requested_by),
                    camera_id=body.camera_id,
                    on_behalf_of=body.on_behalf_of,
                ),
                max_width=body.max_width,
                max_bytes=body.max_bytes,
                timeout_s=body.timeout_s,
            )
        except FrameTapError as exc:
            raise _problem(exc) from exc
        finally:
            slots.release()
        return Response(content=frame.data, media_type="image/jpeg", headers=_frame_headers(frame))

    @router.get(
        "/internal/v1/frames/stream",
        include_in_schema=False,
        dependencies=[Depends(authorized)],
    )
    async def stream(
        account_id: UUID,
        mower_id: UUID,
        camera_id: Annotated[str | None, Query(min_length=1, max_length=64)] = None,
        fps: Annotated[float, Query(gt=0, le=STREAM_MAX_FPS)] = 1.0,
        max_width: Annotated[int, Query(ge=160, le=3840)] = 1280,
    ) -> StreamingResponse:
        current = active_tap()
        await acquire()
        try:
            opened = await current.open_stream(
                FrameRequest(
                    account_id=account_id,
                    mower_id=mower_id,
                    requested_by=_principal(None),
                    camera_id=camera_id,
                ),
                fps=fps,
                max_width=max_width,
            )
        except FrameTapError as exc:
            slots.release()
            raise _problem(exc) from exc
        except BaseException:
            slots.release()
            raise
        released = False

        async def finish() -> None:
            nonlocal released
            await opened.close()
            if not released:
                released = True
                slots.release()

        async def parts(source: FrameStream) -> AsyncIterator[bytes]:
            index = 0
            try:
                async for frame in source.frames():
                    yield _part(frame, index)
                    index += 1
            finally:
                await finish()
                _log.info(
                    "vision frame stream ended",
                    mower_id=str(mower_id),
                    camera=source.camera_id,
                    frames=index,
                )

        return StreamingResponse(
            parts(opened),
            media_type=f"multipart/x-mixed-replace; boundary={BOUNDARY}",
            headers={"Cache-Control": "no-store", "X-Frame-Camera": opened.camera_id},
            background=BackgroundTask(finish),
        )

    return router
