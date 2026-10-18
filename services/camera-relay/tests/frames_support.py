"""Helpers of the frame tap tests: a vendor that goes live, a fake grabber and a JPEG."""

import asyncio
import io
from collections.abc import AsyncGenerator, Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime
from uuid import UUID

from mowdeck_contracts.v1 import utc_now
from mowdeck_contracts.v1.camera import CameraInfo, CameraPlacement
from PIL import Image
from relay_support import FakeVendor

from camera_relay.application.frames import Frame, FrameUnavailableError
from camera_relay.application.ports import ForbiddenSourceError, SourceOffer
from camera_relay.application.service import RelayService
from camera_relay.domain.effects import StartVendorSession, UpdateCameras
from camera_relay.domain.models import RelayKey

FRONT_CAMERAS = (
    CameraInfo(id="front_left", name="Front left", uid=1, placement=CameraPlacement.FRONT_LEFT),
    CameraInfo(id="front_right", name="Front right", uid=2, placement=CameraPlacement.FRONT_RIGHT),
)


def jpeg(width: int = 640, height: int = 480) -> bytes:
    """A small grey JPEG."""
    buffer = io.BytesIO()
    Image.new("RGB", (width, height), (90, 120, 90)).save(buffer, format="JPEG", quality=80)
    return buffer.getvalue()


JPEG = jpeg()


@dataclass
class LiveVendor(FakeVendor):
    """A vendor whose sources go live right after the start (the reconciler's report), and
    that refuses mowers of other accounts like the Device-Gateway (``owner``)."""

    service: RelayService | None = None
    owner: dict[UUID, UUID] = field(default_factory=dict)
    """mower -> the only account the gateway serves it to."""
    quota_remaining_percent: float | None = None
    go_live: bool = True
    silent: set[str] = field(default_factory=set)
    """Listed cameras that never publish (like the LUBA's rear one)."""
    _tasks: set[asyncio.Task[None]] = field(default_factory=set)

    async def offer(self, key: RelayKey) -> SourceOffer:
        owner = self.owner.get(key.mower_id)
        if owner is not None and owner != key.account_id:
            self.offers.append(key)
            raise ForbiddenSourceError("the account does not own the mower")
        offer = await super().offer(key)
        return SourceOffer(
            provider=offer.provider,
            cameras=offer.cameras,
            quota_remaining_percent=self.quota_remaining_percent,
        )

    async def update_cameras(self, command: UpdateCameras) -> None:
        await super().update_cameras(command)
        publishing = [c for c in command.camera_ids if c not in self.silent]
        if self.service is None or not publishing:
            return
        service = self.service

        async def report() -> None:  # later, like the reconciler (the use case holds the lock)
            await asyncio.sleep(0)
            # The first publishing camera takes a starting source live.
            await service.source_started(command.key, command.session_id, self.provider, publishing)
            await service.cameras_published(command.key, command.session_id, publishing)

        task = asyncio.create_task(report())
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def start(self, command: StartVendorSession) -> None:
        await super().start(command)
        if self.go_live and self.service is not None:
            service = self.service

            async def live() -> None:
                await asyncio.sleep(0)
                publishing = [c for c in command.camera_ids if c not in self.silent]
                if publishing:
                    await service.source_started(
                        command.key, command.session_id, self.provider, publishing
                    )

            task = asyncio.create_task(live())
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)


@dataclass
class FakeGrabber:
    """Frames without ffmpeg. ``unavailable`` first tries fail like a path that is not ready;
    ``hang`` makes every snapshot wait forever; ``during`` runs inside each snapshot (to look
    at the lease while the frame is taken); ``after_stream`` runs when a stream's frames are
    used up."""

    stream_frames: int = 3
    unavailable: int = 0
    hang: bool = False
    data: bytes = JPEG
    during: Callable[[str], Awaitable[None]] | None = None
    after_stream: Callable[[], Awaitable[None]] | None = None
    snapshots: list[str] = field(default_factory=list)
    streams: list[tuple[str, float, float]] = field(default_factory=list)
    closed_streams: int = 0
    clock: Callable[[], datetime] = utc_now

    def _frame(self) -> Frame:
        now = self.clock()
        return Frame(data=self.data, received_at=now, captured_at=now, width=640, height=480)

    async def snapshot(self, path: str, *, max_width: int, max_bytes: int) -> Frame:
        del max_width, max_bytes
        self.snapshots.append(path)
        if self.unavailable > 0:
            self.unavailable -= 1
            msg = f"no frame from {path}"
            raise FrameUnavailableError(msg)
        if self.during is not None:
            await self.during(path)
        if self.hang:
            await asyncio.Event().wait()
        return self._frame()

    async def stream(
        self, path: str, *, fps: float, max_width: int, duration: float
    ) -> AsyncGenerator[Frame]:
        del max_width
        self.streams.append((path, fps, duration))
        try:
            for _ in range(self.stream_frames):
                await asyncio.sleep(0)
                yield self._frame()
            if self.after_stream is not None:
                await self.after_stream()
        finally:
            self.closed_streams += 1
