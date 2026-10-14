"""The capture worker's HTTP API: ``POST /api/v1/capture`` (internal, ADR 0035).

The body is a :class:`~mowdeck_contracts.v1.CameraSnapshotRequest` (the camera session with its
secret token, the camera's Agora uid, a timeout, a size cap); the answer is ``image/jpeg``.

* ``Authorization: Bearer <MOWDECK_CAPTURE_TOKEN>`` when a token is configured (401 otherwise).
* At most ``capture_max_concurrency`` captures at once; a request waits up to
  ``capture_queue_seconds`` for a slot, then ``503`` (``Retry-After: 5``).
* ``504`` when no frame arrived within the request's ``timeout_seconds`` (capped by
  ``capture_max_timeout_seconds``), ``502`` when the camera did not publish, no frame could be
  decoded, the browser failed or the JPEG stays above ``max_bytes`` at the lowest quality.

``/healthz``, ``/readyz`` and ``/metrics`` come from the kit. Tokens and images are never logged.
"""

import asyncio
import hmac
import time
from typing import Annotated, ClassVar

from fastapi import APIRouter, Depends, FastAPI, Header, Request, Response
from mowdeck_contracts.v1 import CameraSnapshotRequest, ModuleDescriptor
from mowdeck_service_kit import create_app, get_logger, run
from mowdeck_service_kit.errors import ServiceError, ServiceUnavailable, Unauthorized

from vision import __version__
from vision.capture.grabber import CaptureFailed, FakeGrabber, FrameGrabber, PlaywrightGrabber
from vision.capture.settings import CaptureSettings

_log = get_logger("vision.capture")

DESCRIPTOR = ModuleDescriptor(name="vision-capture", version=__version__)
QUALITY_STEPS = (1.0, 0.75, 0.5)
"""Factors of the configured JPEG quality tried while a frame is above ``max_bytes``."""


class CaptureFailedError(ServiceError):
    """The camera gave no usable frame (502)."""

    status: ClassVar[int] = 502
    problem_type: ClassVar[str] = "capture-failed"
    title: ClassVar[str] = "No frame from the camera"


class CaptureTimeout(ServiceError):  # noqa: N818 - HTTP status names read best
    """No frame in time (504)."""

    status: ClassVar[int] = 504
    problem_type: ClassVar[str] = "capture-timeout"
    title: ClassVar[str] = "No frame from the camera in time"


class Capturer:
    """Concurrency limit, timeouts and the size cap around a :class:`FrameGrabber`."""

    def __init__(self, grabber: FrameGrabber, settings: CaptureSettings) -> None:
        self.grabber = grabber
        self.settings = settings
        self._slots = asyncio.Semaphore(settings.capture_max_concurrency)

    async def capture(self, request: CameraSnapshotRequest) -> bytes:
        """One JPEG within the limits (raises the problem errors above)."""
        try:
            async with asyncio.timeout(self.settings.capture_queue_seconds):
                await self._slots.acquire()
        except TimeoutError:
            raise ServiceUnavailable(
                "every capture slot is busy", headers={"Retry-After": "5"}
            ) from None
        try:
            timeout = min(request.timeout_seconds, self.settings.capture_max_timeout_seconds)
            bounded = request.model_copy(update={"timeout_seconds": timeout})
            for factor in QUALITY_STEPS:
                quality = max(0.1, self.settings.capture_jpeg_quality * factor)
                try:
                    # The page has its own deadline; this one also covers the browser start.
                    async with asyncio.timeout(timeout + 10):
                        data = await self.grabber.grab(bounded, quality=quality)
                except TimeoutError:
                    raise CaptureTimeout("no frame within the timeout") from None
                except CaptureFailed as exc:
                    raise CaptureFailedError(f"no frame from the camera: {exc.reason}") from None
                if len(data) <= request.max_bytes:
                    return data
            raise CaptureFailedError("the frame stays above max_bytes")
        finally:
            self._slots.release()


def grabber_from(settings: CaptureSettings) -> FrameGrabber:
    """The configured grabber."""
    if settings.capture_backend == "fake":
        return FakeGrabber()
    return PlaywrightGrabber(
        sdk=settings.capture_agora_sdk,
        channel=settings.capture_browser_channel,
        settle_ms=settings.capture_settle_ms,
        profile=settings.capture_agora_profile,
    )


router = APIRouter(tags=["capture"])


def _capturer(request: Request) -> Capturer:
    capturer: Capturer = request.app.state.capturer
    return capturer


def _check_token(request: Request, authorization: Annotated[str | None, Header()] = None) -> None:
    token = request.app.state.capture_settings.capture_token
    if token is None or not token.get_secret_value():
        return
    expected = f"Bearer {token.get_secret_value()}"
    if authorization is None or not hmac.compare_digest(authorization, expected):
        raise Unauthorized("a valid capture token is required")


@router.post(
    "/capture",
    dependencies=[Depends(_check_token)],
    responses={200: {"content": {"image/jpeg": {}}, "description": "The frame as JPEG."}},
)
async def capture(
    body: CameraSnapshotRequest, capturer: Annotated[Capturer, Depends(_capturer)]
) -> Response:
    """One still frame of the camera ``camera_uid`` (None: the first publisher)."""
    started = time.monotonic()
    data = await capturer.capture(body)
    _log.info(
        "frame captured",
        channel_uid=body.camera_uid,
        bytes=len(data),
        latency_ms=round((time.monotonic() - started) * 1000),
    )
    return Response(content=data, media_type="image/jpeg", headers={"Cache-Control": "no-store"})


def build_capture_app(settings: CaptureSettings, *, grabber: FrameGrabber | None = None) -> FastAPI:
    """The worker app (tests pass their own grabber)."""
    active = grabber or grabber_from(settings)

    async def startup(app: FastAPI) -> None:
        app.state.capturer = Capturer(active, settings)
        _log.info(
            "capture worker started",
            backend=settings.capture_backend,
            concurrency=settings.capture_max_concurrency,
            token=settings.capture_token is not None,
        )

    async def shutdown(app: FastAPI) -> None:
        await active.close()

    app = create_app(
        settings,
        DESCRIPTOR,
        routers=[router],
        on_startup=[startup],
        on_shutdown=[shutdown],
    )
    app.state.capture_settings = settings
    return app


def main() -> None:
    """Serve the worker with the settings from the environment."""
    settings = CaptureSettings.from_env()
    run(build_capture_app(settings), settings)


__all__ = ["Capturer", "build_capture_app", "grabber_from", "main"]
