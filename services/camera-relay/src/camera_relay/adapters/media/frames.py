"""The ``FrameGrabber`` port with ffmpeg: frames of a relay path as JPEG.

Frames are read from the viewer path ``r/<relay_id>/<camera>`` (H.264 with a 1 s GOP, so the
first decodable frame arrives within about a second) over RTSP inside the cluster with the
internal publisher credential, like the transcoder; the media server asks the relay for that
read like for every other.

* **Snapshot.** ``ffmpeg ... -frames:v 1 -c:v png``: the first decoded frame (ffmpeg's H.264
  decoder shows nothing before a key frame), scaled down to ``max_width`` (never up, even
  height), as a lossless PNG; Pillow encodes it as JPEG and lowers the quality while it is
  above ``max_bytes``, with the quality steps of ADR 0035's capture worker (0.8 times 1, 0.75
  and 0.5).
* **Stream.** ``ffmpeg ... -c:v mjpeg -f image2pipe``: every frame at least ``1/fps`` after the
  previous one is selected (``select`` with ``-fps_mode vfr``: frames are dropped, never
  repeated, so a frozen source sends nothing), scaled like the snapshot and split into JPEG
  images at their end markers. ``-t`` bounds the stream; closing the generator kills ffmpeg.

The RTSP URL carries the internal credential: it never reaches a log (stderr is redacted).
"""

import asyncio
import contextlib
import io
from collections.abc import AsyncGenerator, Callable
from datetime import datetime
from typing import Final

from mowdeck_contracts.v1 import utc_now
from mowdeck_service_kit import get_logger
from PIL import Image

from camera_relay.adapters.media.pipeline import MediaServer
from camera_relay.application.frames import Frame, FrameTooLargeError, FrameUnavailableError

_log = get_logger("camera_relay.media.frames")

JPEG_QUALITY: Final = 0.8
"""JPEG quality of a frame (0 to 1), as the capture worker's default."""
QUALITY_STEPS: Final = (1.0, 0.75, 0.5)
"""Factors of :data:`JPEG_QUALITY` tried while a snapshot is above ``max_bytes``."""
STREAM_QSCALE: Final = 5
"""ffmpeg's MJPEG quantizer of stream frames (2 best to 31 worst; 5 is about quality 0.8)."""
SOI: Final = b"\xff\xd8"
EOI: Final = b"\xff\xd9"
READ_CHUNK: Final = 64 * 1024
MAX_FRAME_BYTES: Final = 8 * 1024 * 1024
"""A stream part larger than this is not a frame of these cameras: the reader stops."""

COMMON: Final = ("-hide_banner", "-nostdin", "-loglevel", "error")


def _scale(max_width: int) -> str:
    return f"scale=w='min({max_width},iw)':h=-2"


def snapshot_args(*, ffmpeg: str, input_url: str, max_width: int) -> list[str]:
    """One decoded frame of ``input_url`` as PNG on stdout."""
    return [
        ffmpeg,
        *COMMON,
        "-rtsp_transport",
        "tcp",
        "-i",
        input_url,
        "-an",
        "-frames:v",
        "1",
        "-vf",
        _scale(max_width),
        "-f",
        "image2pipe",
        "-c:v",
        "png",
        "pipe:1",
    ]


def stream_args(
    *, ffmpeg: str, input_url: str, fps: float, max_width: int, duration: float
) -> list[str]:
    """JPEG frames of ``input_url``, at most ``fps`` per second, for ``duration`` seconds."""
    interval = 1.0 / fps
    select = f"select='isnan(prev_selected_t)+gte(t-prev_selected_t,{interval:.3f})'"
    return [
        ffmpeg,
        *COMMON,
        "-fflags",
        "nobuffer",
        "-flags",
        "low_delay",
        "-rtsp_transport",
        "tcp",
        "-i",
        input_url,
        "-an",
        "-t",
        f"{max(duration, 0.1):.3f}",
        "-vf",
        f"{select},{_scale(max_width)}",
        "-fps_mode",
        "vfr",
        "-c:v",
        "mjpeg",
        "-pix_fmt",
        "yuvj420p",
        "-q:v",
        str(STREAM_QSCALE),
        "-f",
        "image2pipe",
        "pipe:1",
    ]


def encode_jpeg(image: Image.Image, *, max_bytes: int) -> tuple[bytes, float]:
    """``image`` as JPEG at the highest quality step within ``max_bytes``; raises
    :class:`FrameTooLargeError` when even the lowest step is too large."""
    rgb = image.convert("RGB")
    size = 0
    for factor in QUALITY_STEPS:
        quality = max(0.1, JPEG_QUALITY * factor)
        buffer = io.BytesIO()
        rgb.save(buffer, format="JPEG", quality=round(quality * 100))
        data = buffer.getvalue()
        if len(data) <= max_bytes:
            return data, quality
        size = len(data)
    msg = f"the frame has {size} bytes at the lowest quality, more than {max_bytes}"
    raise FrameTooLargeError(msg)


def split_jpegs(buffer: bytearray) -> list[bytes]:
    """Cut every complete JPEG (``FFD8`` ... ``FFD9``) off the front of ``buffer``.

    Inside an image an ``FF`` byte of the entropy coded data is always followed by ``00`` and
    MJPEG frames of ffmpeg carry no thumbnail, so the first end marker after a start marker
    closes the image."""
    images: list[bytes] = []
    while True:
        start = buffer.find(SOI)
        if start < 0:
            buffer.clear()
            return images
        end = buffer.find(EOI, start + 2)
        if end < 0:
            if start:
                del buffer[:start]
            return images
        images.append(bytes(buffer[start : end + 2]))
        del buffer[: end + 2]


def _dimensions(data: bytes) -> tuple[int | None, int | None]:
    try:
        with Image.open(io.BytesIO(data)) as image:
            return image.width, image.height
    except OSError:
        return None, None


class FfmpegFrameGrabber:
    """Reads frames of relay paths with ffmpeg."""

    def __init__(
        self,
        *,
        server: MediaServer,
        ffmpeg: str = "ffmpeg",
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self.server = server
        self.ffmpeg = ffmpeg
        self._clock = clock

    def _redact(self, text: str) -> str:
        for secret in self.server.secrets():
            if secret:
                text = text.replace(secret, "<redacted>")
        return text

    async def _spawn(self, args: list[str]) -> asyncio.subprocess.Process:
        try:
            return await asyncio.create_subprocess_exec(
                *args,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except OSError as exc:
            msg = f"ffmpeg did not start: {type(exc).__name__}"
            raise FrameUnavailableError(msg) from exc

    async def snapshot(self, path: str, *, max_width: int, max_bytes: int) -> Frame:
        """One JPEG of ``path``."""
        process = await self._spawn(
            snapshot_args(ffmpeg=self.ffmpeg, input_url=self.server.url(path), max_width=max_width)
        )
        try:
            png, errors = await process.communicate()
        finally:
            await _end(process)
        received_at = self._clock()
        if process.returncode != 0 or not png:
            detail = self._redact(errors.decode("utf-8", "replace").strip())[-300:]
            _log.debug("no frame from the path", path=path, error=detail)
            msg = f"no frame from {path}"
            raise FrameUnavailableError(msg)
        try:
            with Image.open(io.BytesIO(png)) as image:
                image.load()
                width, height = image.width, image.height
                data, quality = encode_jpeg(image, max_bytes=max_bytes)
        except OSError as exc:
            msg = "the decoded frame is not an image"
            raise FrameUnavailableError(msg) from exc
        _log.debug("frame grabbed", path=path, width=width, height=height, quality=quality)
        return Frame(
            data=data,
            received_at=received_at,
            captured_at=received_at,
            width=width,
            height=height,
        )

    async def stream(
        self, path: str, *, fps: float, max_width: int, duration: float
    ) -> AsyncGenerator[Frame]:
        """JPEG frames of ``path`` until it ends, ``duration`` passed or the caller closes."""
        process = await self._spawn(
            stream_args(
                ffmpeg=self.ffmpeg,
                input_url=self.server.url(path),
                fps=fps,
                max_width=max_width,
                duration=duration,
            )
        )
        assert process.stdout is not None  # noqa: S101 - piped
        buffer = bytearray()
        sent = 0
        try:
            while True:
                chunk = await process.stdout.read(READ_CHUNK)
                if not chunk:
                    break
                buffer.extend(chunk)
                if len(buffer) > MAX_FRAME_BYTES:
                    _log.warning("frame stream part too large; reader stopped", path=path)
                    break
                for data in split_jpegs(buffer):
                    received_at = self._clock()
                    width, height = _dimensions(data)
                    sent += 1
                    yield Frame(
                        data=data,
                        received_at=received_at,
                        captured_at=received_at,
                        width=width,
                        height=height,
                    )
        finally:
            await _end(process)
        if sent == 0:
            errors = b""
            if process.stderr is not None:
                with contextlib.suppress(Exception):
                    errors = await process.stderr.read()
            detail = self._redact(errors.decode("utf-8", "replace").strip())[-300:]
            _log.debug("no frame stream from the path", path=path, error=detail)
            msg = f"no frames from {path}"
            raise FrameUnavailableError(msg)


async def _end(process: asyncio.subprocess.Process) -> None:
    """Kill a still running ffmpeg and reap it."""
    if process.returncode is None:
        with contextlib.suppress(ProcessLookupError):
            process.kill()
    with contextlib.suppress(Exception):
        await asyncio.wait_for(process.wait(), timeout=5)
