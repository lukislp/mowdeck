"""The frame tap: still frames and frame streams of a relay session for Vision (ADR 0045).

Vision (through the Device-Gateway) gets its camera frames from the relay instead of a headless
browser per capture (ADR 0035's worker). Every frame request holds a ``vision`` lease for
exactly its own lifetime:

* **Snapshot.** :meth:`FrameTap.snapshot` opens a lease (``VisionMode.SNAPSHOT``: the domain
  caps it at ``VISION_SNAPSHOT_HOLD_SECONDS``, 30 s, never extended), waits until the session is
  live, takes one frame of the camera's H.264 path and detaches, also when the caller gave up
  (timeout, cancellation): the detach is shielded.
* **Frame set.** :meth:`FrameTap.snapshot_set` opens one snapshot lease on up to two cameras
  (both front cameras when the mower has a left and a right one), reads every camera at
  :data:`SET_FPS` at the same time and serves the frames captured closest together (target
  ``VISION_FRAME_SET_TARGET_SKEW_MS``, 200 ms), left before right. A camera that is silent or
  gives no frame within :data:`SET_PARTNER_WAIT_SECONDS` of the first frame is left out and
  named in ``missing``; the set fails only without any frame.
* **Stream.** :meth:`FrameTap.open_stream` opens a lease (``VisionMode.STREAM``) and returns a
  :class:`FrameStream`; every frame the caller pulls renews the lease (at most every
  :data:`STREAM_RENEW_SECONDS`), the stream ends after :data:`STREAM_HARD_CAP_SECONDS` (300 s)
  whatever happens, when the lease is closed (a Vision limit, the mower went offline) or when
  the caller closes it. A caller that stops pulling stops renewing.

Reuse and limits come from the session domain: with a viewer present the running session is
used as it is (no second vendor session, no Vision minutes); without one a Vision lease starts
the source only within the Vision limits (never an offline or sleeping mower; ``budget`` below
15 % battery unless charging, below 10 % of the video budget, after 20 Vision minutes per
account, mower and UTC day). After the last lease the session drains for the grace and is
released like any other.

The lease belongs to the requesting service principal (strict leases); the account and the mower
come from the caller, which is a service that already checked them (the Device-Gateway takes
them from its own connection). A key is one ``(account_id, mower_id)``, so a request of account
B never reaches a session of account A.
"""

import asyncio
import contextlib
import itertools
import time
from collections import deque
from collections.abc import AsyncGenerator, AsyncIterator, Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Final, Protocol
from uuid import UUID

from mowdeck_contracts.v1.camera import CameraInfo
from mowdeck_contracts.v1.principal import PrincipalRef
from mowdeck_contracts.v1.relay import (
    VISION_FRAME_SET_MAX_FRAMES,
    VISION_FRAME_SET_TARGET_SKEW_MS,
    VISION_SNAPSHOT_HOLD_SECONDS,
    VISION_STREAM_LEASE_SECONDS,
    CameraSide,
    RelayAttachRequest,
    RelayDetachRequest,
    RelayErrorCode,
    RelayPurpose,
    RelayRenewRequest,
    VisionDemand,
    VisionFrameMissingReason,
    camera_side,
)
from mowdeck_service_kit import get_logger

from camera_relay.application.service import RelayService
from camera_relay.domain.models import RelayKey, SessionState, VisionMode
from camera_relay.domain.tickets import camera_path

_log = get_logger("camera_relay.frames")

STREAM_HARD_CAP_SECONDS: Final = float(VISION_STREAM_LEASE_SECONDS)
"""A frame stream ends this long after it opened, however much it is pulled."""
STREAM_RENEW_SECONDS: Final = 5.0
"""A pulled stream renews its lease at most this often."""
STREAM_MAX_FPS: Final = 5.0
"""Highest frame rate of a stream (the sources run at about 10 fps)."""
SNAPSHOT_MAX_SECONDS: Final = float(VISION_SNAPSHOT_HOLD_SECONDS)
"""Longest snapshot request: the snapshot lease never holds the session longer."""
READY_POLL_SECONDS: Final = 0.2
"""How often a request looks whether its session went live."""
RETRY_SECONDS: Final = 0.3
"""Wait between two tries to read a camera path that is not ready yet."""
SET_FPS: Final = 10.0
"""Read rate of each camera while a frame set is assembled (the sources run at about 10 fps,
so the closest frames of two cameras lie at most about 50 ms apart)."""
SET_WINDOW_SECONDS: Final = 1.5
"""Once every camera of a set delivered, how long to look for frames closer together than the
target before the closest found are served."""
SET_PARTNER_WAIT_SECONDS: Final = 5.0
"""After the first frame of a set, how long to wait for the other cameras (a key frame, a camera
the running session only now switches on) before serving the set without them."""
SET_BUFFER_FRAMES: Final = 12
"""Recent frames kept per camera to pick the closest set from."""


@dataclass(frozen=True, slots=True)
class Frame:
    """One encoded frame (JPEG) with the relay's wall clock times."""

    data: bytes
    received_at: datetime
    """When the relay had the decoded frame."""
    captured_at: datetime
    """The best estimate of when the camera took it (the sender's clock is not wall time,
    ADR 0045; without a better estimate the receive time)."""
    width: int | None = None
    height: int | None = None


class FrameUnavailableError(Exception):
    """The path gave no frame (yet): not ready, the reader failed. Retried until the deadline."""


class FrameTooLargeError(Exception):
    """The frame stays above ``max_bytes`` at the lowest JPEG quality."""


@dataclass(frozen=True, slots=True)
class SetFrame:
    """One camera's frame of a :class:`FrameSet`."""

    camera: CameraInfo
    frame: Frame

    @property
    def side(self) -> CameraSide | None:
        """``left`` or ``right`` for a front camera of a stereo pair."""
        return camera_side(self.camera.placement)


@dataclass(frozen=True, slots=True)
class MissingCamera:
    """A camera of a set that gave no frame."""

    camera: CameraInfo
    reason: VisionFrameMissingReason

    @property
    def side(self) -> CameraSide | None:
        """``left`` or ``right`` for a front camera of a stereo pair."""
        return camera_side(self.camera.placement)


@dataclass(frozen=True, slots=True)
class FrameSet:
    """Frames of the same moment, left before right, and the cameras without one."""

    frames: tuple[SetFrame, ...]
    missing: tuple[MissingCamera, ...] = ()

    @property
    def skew_ms(self) -> float | None:
        """Milliseconds between the first and the last capture time; None for one frame."""
        if len(self.frames) < 2:
            return None
        times = [item.frame.captured_at for item in self.frames]
        return (max(times) - min(times)).total_seconds() * 1000


class FrameGrabber(Protocol):
    """Reads frames of a media path (``r/<relay_id>/<camera>``, H.264)."""

    async def snapshot(self, path: str, *, max_width: int, max_bytes: int) -> Frame:
        """One JPEG of ``path``, at most ``max_width`` wide and ``max_bytes`` large; raises
        :class:`FrameUnavailableError` or :class:`FrameTooLargeError`. The caller bounds the
        time."""
        ...

    def stream(
        self, path: str, *, fps: float, max_width: int, duration: float
    ) -> AsyncGenerator[Frame]:
        """JPEG frames of ``path`` at most ``fps`` per second (never a repeated frame) for at
        most ``duration`` seconds; ends when the path ends. Raises
        :class:`FrameUnavailableError` before the first frame when the path cannot be read.
        Closing the generator ends the reader."""
        ...

    async def fit(self, frame: Frame, *, max_bytes: int) -> Frame:
        """``frame`` re-encoded at a lower JPEG quality to at most ``max_bytes``; raises
        :class:`FrameTooLargeError` when even the lowest quality is too large."""
        ...


class FrameTapError(Exception):
    """No frame for this request; ``code`` says why (the HTTP status follows from it)."""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(detail)
        self.code = code
        self.detail = detail


TIMEOUT: Final = "timeout"
"""No frame within the request's time."""
FAILED: Final = "failed"
"""The camera path gave no usable frame (or none small enough)."""


@dataclass(frozen=True, slots=True)
class FrameRequest:
    """Who wants frames of which camera."""

    account_id: UUID
    mower_id: UUID
    requested_by: PrincipalRef
    """The requesting service (it owns the lease)."""
    camera_id: str | None = None
    """None: the camera most likely to deliver (one already publishing, else a front
    camera; never a listed camera known to be silent)."""
    on_behalf_of: UUID | None = None
    """The user the service acts for (logged)."""
    demand: VisionDemand = VisionDemand.ROUTINE
    """``error_analysis`` (a mower with a problem) may wake a sleeping mower and is not bound by
    the Vision budget; ``routine`` keeps every limit."""

    @property
    def key(self) -> RelayKey:
        """The session key of the request."""
        return RelayKey(self.account_id, self.mower_id)


@dataclass(slots=True)
class _Lease:
    request: FrameRequest
    lease_id: UUID
    camera_id: str
    path: str
    closed: bool = False
    cameras: tuple[CameraInfo, ...] = ()
    """Every camera of the lease (a frame set holds more than one)."""
    paths: dict[str, str] = field(default_factory=dict)
    """The media path of each camera of the lease."""


@dataclass(slots=True)
class _SetCamera:
    """What a frame set has of one camera so far."""

    info: CameraInfo
    path: str
    frames: deque[Frame] = field(default_factory=lambda: deque(maxlen=SET_BUFFER_FRAMES))
    first_at: float | None = None
    """Monotonic time of its first frame."""
    silent: bool = False
    error: FrameTapError | None = None
    done: bool = False
    """The camera's reader ended (silent, failed, the deadline passed)."""


_SIDE_ORDER: Final[dict[str, int]] = {"left": 0, "right": 1}


def _set_order(camera: CameraInfo) -> int:
    """Left before right before any other camera."""
    side = camera_side(camera.placement)
    return _SIDE_ORDER[side] if side else len(_SIDE_ORDER)


def closest_frames(buffers: Sequence[Sequence[Frame]]) -> tuple[list[Frame], float]:
    """One frame of each buffer, captured as close together as possible (ties: the newest),
    and their spread in seconds. Every buffer holds at least one frame."""
    best: list[Frame] = []
    best_key: tuple[float, float] | None = None
    for combination in itertools.product(*buffers):
        times = [frame.captured_at for frame in combination]
        spread = (max(times) - min(times)).total_seconds()
        key = (spread, -max(times).timestamp())
        if best_key is None or key < best_key:
            best, best_key = list(combination), key
    return best, best_key[0] if best_key is not None else 0.0


class FrameTap:
    """Frames of relay sessions under ``vision`` leases."""

    def __init__(
        self,
        service: RelayService,
        grabber: FrameGrabber,
        *,
        monotonic: Callable[[], float] = time.monotonic,
        ready_poll: float = READY_POLL_SECONDS,
        retry_wait: float = RETRY_SECONDS,
        set_window: float = SET_WINDOW_SECONDS,
        partner_wait: float = SET_PARTNER_WAIT_SECONDS,
    ) -> None:
        self.service = service
        self.grabber = grabber
        self._monotonic = monotonic
        self._ready_poll = ready_poll
        self._retry_wait = retry_wait
        self._set_window = set_window
        self._partner_wait = partner_wait
        self._detaching: set[asyncio.Task[None]] = set()

    # --- Snapshot -----------------------------------------------------------------------------

    async def snapshot(
        self, request: FrameRequest, *, max_width: int, max_bytes: int, timeout_s: float
    ) -> Frame:
        """One frame; the lease lives exactly as long as this call (at most 30 s)."""
        budget = min(timeout_s, SNAPSHOT_MAX_SECONDS)
        deadline = self._monotonic() + budget
        holder: list[_Lease] = []
        try:
            async with asyncio.timeout(max(deadline - self._monotonic(), 0.0)):
                lease = await self._ready_lease(request, VisionMode.SNAPSHOT, holder)
                frame = await self._first_frame(lease, max_width=max_width, max_bytes=max_bytes)
        except TimeoutError as exc:
            raise FrameTapError(TIMEOUT, "no camera frame in time") from exc
        finally:
            for held in holder:
                await self._detach(held)
        _log.info(
            "vision frame served",
            mower_id=str(request.mower_id),
            camera=lease.camera_id,
            lease_id=str(lease.lease_id),
            on_behalf_of=str(request.on_behalf_of) if request.on_behalf_of else None,
            demand=request.demand.value,
            bytes=len(frame.data),
        )
        return frame

    async def _first_frame(self, lease: _Lease, *, max_width: int, max_bytes: int) -> Frame:
        while True:
            self._require_open(lease)
            try:
                return await self.grabber.snapshot(
                    lease.path, max_width=max_width, max_bytes=max_bytes
                )
            except FrameTooLargeError as exc:
                raise FrameTapError(FAILED, str(exc) or "the frame is too large") from exc
            except FrameUnavailableError:
                await asyncio.sleep(self._retry_wait)

    # --- Frame set ----------------------------------------------------------------------------

    async def snapshot_set(
        self,
        request: FrameRequest,
        *,
        max_frames: int = VISION_FRAME_SET_MAX_FRAMES,
        max_width: int,
        max_bytes: int,
        timeout_s: float,
    ) -> FrameSet:
        """Frames of the same moment from up to ``max_frames`` cameras (``max_bytes`` each);
        the lease lives exactly as long as this call (at most 30 s). Fails only when no camera
        gives a frame; a camera that is silent or late is named in ``missing``."""
        deadline = self._monotonic() + min(timeout_s, SNAPSHOT_MAX_SECONDS)
        holder: list[_Lease] = []
        avoid: list[str] = []
        try:
            while True:
                try:
                    async with asyncio.timeout(max(deadline - self._monotonic(), 0.0)):
                        lease = await self._attach(
                            request, VisionMode.SNAPSHOT, avoid=avoid, count=max_frames
                        )
                except TimeoutError as exc:
                    raise FrameTapError(TIMEOUT, "no camera frame in time") from exc
                holder.append(lease)
                if avoid and all(camera.id in avoid for camera in lease.cameras):
                    raise FrameTapError(
                        RelayErrorCode.UNAVAILABLE.value, "no camera of the mower publishes"
                    )
                found = await self._collect_set(
                    lease, max_width=max_width, max_bytes=max_bytes, deadline=deadline
                )
                if found.frames:
                    break
                # Every camera of the lease is silent: try the others, like a single snapshot.
                holder.remove(lease)
                await self._detach(lease)
                if request.camera_id is not None:
                    raise FrameTapError(
                        RelayErrorCode.UNAVAILABLE.value,
                        f"camera {lease.camera_id} does not publish",
                    )
                avoid.extend(camera.id for camera in lease.cameras)
                _log.info(
                    "vision cameras silent, trying others",
                    mower_id=str(request.mower_id),
                    cameras=[camera.id for camera in lease.cameras],
                )
        finally:
            for held in holder:
                await self._detach(held)
        skew = found.skew_ms
        _log.info(
            "vision frame set served",
            mower_id=str(request.mower_id),
            cameras=[item.camera.id for item in found.frames],
            missing=[f"{item.camera.id}:{item.reason.value}" for item in found.missing],
            skew_ms=round(skew) if skew is not None else None,
            lease_id=str(lease.lease_id),
            on_behalf_of=str(request.on_behalf_of) if request.on_behalf_of else None,
            demand=request.demand.value,
            bytes=sum(len(item.frame.data) for item in found.frames),
        )
        return found

    async def _collect_set(
        self, lease: _Lease, *, max_width: int, max_bytes: int, deadline: float
    ) -> FrameSet:
        """Read every camera of ``lease`` at once until the closest frames are good enough (or
        the time is up) and serve them; an empty set when every camera is silent."""
        ordered = sorted(enumerate(lease.cameras), key=lambda item: (_set_order(item[1]), item[0]))
        cameras = [_SetCamera(info=info, path=lease.paths[info.id]) for _, info in ordered]
        wake = asyncio.Event()
        readers = [
            asyncio.create_task(self._read_set_camera(lease, camera, wake, max_width, deadline))
            for camera in cameras
        ]
        try:
            chosen = await self._await_set(cameras, wake, deadline)
        finally:
            for reader in readers:
                reader.cancel()
            await asyncio.gather(*readers, return_exceptions=True)
        if not chosen:
            errors = [camera.error for camera in cameras if camera.error is not None]
            if errors:
                raise errors[0]
            if not all(camera.silent for camera in cameras):
                raise FrameTapError(TIMEOUT, "no camera frame in time")
            return FrameSet(frames=())
        frames: list[SetFrame] = []
        missing: list[MissingCamera] = []
        for camera in cameras:
            frame = chosen.get(camera.info.id)
            if frame is None:
                reason = (
                    VisionFrameMissingReason.SILENT
                    if camera.silent
                    else VisionFrameMissingReason.NO_FRAME
                )
                missing.append(MissingCamera(camera.info, reason))
                continue
            if len(frame.data) > max_bytes:
                try:
                    frame = await self.grabber.fit(frame, max_bytes=max_bytes)
                except FrameTooLargeError:
                    missing.append(MissingCamera(camera.info, VisionFrameMissingReason.TOO_LARGE))
                    continue
            frames.append(SetFrame(camera.info, frame))
        if not frames:
            raise FrameTapError(FAILED, "every frame of the set is too large")
        return FrameSet(frames=tuple(frames), missing=tuple(missing))

    async def _await_set(
        self, cameras: list[_SetCamera], wake: asyncio.Event, deadline: float
    ) -> dict[str, Frame]:
        """Wait until the frames read so far make a good set; the chosen frame per camera id
        (empty: none). Checked on every new frame:

        * every camera that is still read has frames: serve the closest set when it lies within
          the target skew, else once :data:`SET_WINDOW_SECONDS` passed since the last camera's
          first frame, at the latest at the deadline;
        * some cameras have frames, others none yet: serve without them once
          :data:`SET_PARTNER_WAIT_SECONDS` passed since the first frame or at the deadline;
        * no frame at all: give up when every camera is done or at the deadline.
        """
        target = VISION_FRAME_SET_TARGET_SKEW_MS / 1000
        while True:
            now = self._monotonic()
            have = [camera for camera in cameras if camera.frames]
            waiting = [camera for camera in cameras if not camera.frames and not camera.done]
            wake_at = deadline
            if have and not waiting:
                frames, spread = closest_frames([camera.frames for camera in have])
                complete_at = max(camera.first_at or now for camera in have)
                if spread <= target or now >= min(complete_at + self._set_window, deadline):
                    return {c.info.id: f for c, f in zip(have, frames, strict=True)}
                wake_at = min(deadline, complete_at + self._set_window)
            elif have:
                first_at = min(camera.first_at or now for camera in have)
                if now >= min(first_at + self._partner_wait, deadline):
                    frames, _ = closest_frames([camera.frames for camera in have])
                    return {c.info.id: f for c, f in zip(have, frames, strict=True)}
                wake_at = min(deadline, first_at + self._partner_wait)
            elif not waiting or now >= deadline:
                return {}
            wake.clear()
            with contextlib.suppress(TimeoutError):
                async with asyncio.timeout(max(wake_at - now, 0.0) + 0.001):
                    await wake.wait()

    async def _read_set_camera(
        self,
        lease: _Lease,
        camera: _SetCamera,
        wake: asyncio.Event,
        max_width: int,
        deadline: float,
    ) -> None:
        """Keep ``camera``'s recent frames until cancelled, the deadline or the end of the
        lease; marks it silent when it turns out not to publish."""
        try:
            if not await self._wait_ready(lease, camera.info.id):
                camera.silent = True
                return
            while True:
                self._require_open(lease)
                remaining = deadline - self._monotonic()
                if remaining <= 0:
                    return
                reader = self.grabber.stream(
                    camera.path, fps=SET_FPS, max_width=max_width, duration=remaining
                )
                try:
                    async with contextlib.aclosing(reader) as frames:
                        async for frame in frames:
                            camera.frames.append(frame)
                            if camera.first_at is None:
                                camera.first_at = self._monotonic()
                            wake.set()
                except FrameUnavailableError:
                    pass
                await asyncio.sleep(self._retry_wait)
        except FrameTapError as exc:
            camera.error = exc
        finally:
            camera.done = True
            wake.set()

    # --- Stream -------------------------------------------------------------------------------

    async def open_stream(
        self, request: FrameRequest, *, fps: float, max_width: int, ready_timeout: float = 30.0
    ) -> "FrameStream":
        """Open a lease and wait until its session is live; the caller pulls frames from the
        returned :class:`FrameStream` and must close it (closing is idempotent)."""
        rate = min(max(fps, 0.1), STREAM_MAX_FPS)
        holder: list[_Lease] = []
        try:
            async with asyncio.timeout(ready_timeout):
                lease = await self._ready_lease(request, VisionMode.STREAM, holder)
        except TimeoutError as exc:
            for held in holder:
                await self._detach(held)
            raise FrameTapError(TIMEOUT, "the camera did not go live in time") from exc
        except BaseException:
            for held in holder:
                await self._detach(held)
            raise
        return FrameStream(self, lease, fps=rate, max_width=max_width)

    async def _ready_lease(
        self, request: FrameRequest, mode: VisionMode, holder: list[_Lease]
    ) -> _Lease:
        """A lease whose camera delivers. Like the transcoder, wait until the camera's path is
        ready (the session reports the camera available). A camera that turns out silent (a
        listed camera that never publishes) fails a request that named it; without a named
        camera the next candidate is tried, the silent one avoided. ``holder`` keeps the open
        lease for the caller's cleanup."""
        silent: list[str] = []
        while True:
            lease = await self._attach(request, mode, avoid=silent)
            holder.append(lease)
            if await self._wait_ready(lease):
                return lease
            holder.remove(lease)
            await self._detach(lease)
            if request.camera_id is not None:
                raise FrameTapError(
                    RelayErrorCode.UNAVAILABLE.value,
                    f"camera {lease.camera_id} does not publish",
                )
            silent.append(lease.camera_id)
            _log.info(
                "vision camera silent, trying another",
                mower_id=str(request.mower_id),
                camera=lease.camera_id,
            )

    # --- Leases -------------------------------------------------------------------------------

    async def _attach(
        self,
        request: FrameRequest,
        mode: VisionMode,
        *,
        avoid: list[str] | None = None,
        count: int = 1,
    ) -> _Lease:
        reply = await self.service.attach(
            RelayAttachRequest(
                account_id=request.account_id,
                mower_id=request.mower_id,
                camera_ids=[request.camera_id] if request.camera_id else [],
                requested_by=request.requested_by,
                purpose=RelayPurpose.VISION,
                vision_demand=request.demand,
            ),
            vision_mode=mode,
            first_camera_only=True,
            camera_count=count,
            avoid_cameras=tuple(avoid or ()),
        )
        if reply.error is not None:
            _log.info(
                "vision frames refused",
                mower_id=str(request.mower_id),
                code=reply.error.code.value,
                detail=reply.error.detail,
            )
            raise FrameTapError(reply.error.code.value, reply.error.detail)
        relay = reply.session.relay if reply.session is not None else None
        session = self.service.repository.get(request.key)
        if relay is None or not relay.tiles or session is None or session.relay_id is None:
            raise FrameTapError(RelayErrorCode.UNAVAILABLE.value, "no relay session")
        relay_id = session.relay_id
        infos = {info.id: info for info in reply.session.cameras} if reply.session else {}
        cameras = tuple(
            infos.get(tile.camera_id) or CameraInfo(id=tile.camera_id, name=tile.camera_id)
            for tile in relay.tiles
        )
        camera = cameras[0].id
        return _Lease(
            request=request,
            lease_id=relay.lease_id,
            camera_id=camera,
            path=camera_path(relay_id, camera),
            cameras=cameras,
            paths={info.id: camera_path(relay_id, info.id) for info in cameras},
        )

    async def _detach(self, lease: _Lease) -> None:
        """End the lease, also when the caller is being cancelled: the detach runs as a task
        of its own and is awaited shielded, so a cancelled caller (a client that went away, a
        timeout) never leaves the lease open."""
        if lease.closed:
            return
        lease.closed = True
        task = asyncio.ensure_future(self._detach_now(lease))
        self._detaching.add(task)
        task.add_done_callback(self._detaching.discard)
        await asyncio.shield(task)

    async def _detach_now(self, lease: _Lease) -> None:
        request = lease.request
        try:
            await self.service.detach(
                RelayDetachRequest(
                    account_id=request.account_id,
                    mower_id=request.mower_id,
                    lease_id=lease.lease_id,
                    requested_by=request.requested_by,
                )
            )
        except Exception:
            _log.exception("vision lease not detached", lease_id=str(lease.lease_id))

    async def drain(self) -> None:
        """Wait for detaches still running (shutdown, tests).

        A finished task leaves the set in its done callback on a later loop turn; ``gather`` over
        finished tasks does not yield, so only running tasks are awaited (else one yield).
        """
        while self._detaching:
            running = [task for task in self._detaching if not task.done()]
            if running:
                await asyncio.gather(*running, return_exceptions=True)
            else:
                await asyncio.sleep(0)

    async def _renew(self, lease: _Lease) -> bool:
        """Extend a stream lease; False when it is gone."""
        request = lease.request
        reply = await self.service.renew(
            RelayRenewRequest(
                account_id=request.account_id,
                mower_id=request.mower_id,
                lease_id=lease.lease_id,
                requested_by=request.requested_by,
            )
        )
        return reply.error is None

    def _lease_open(self, lease: _Lease) -> bool:
        session = self.service.repository.get(lease.request.key)
        return session is not None and lease.lease_id in session.leases

    def _require_open(self, lease: _Lease) -> None:
        if not self._lease_open(lease):
            raise FrameTapError(
                RelayErrorCode.UNAVAILABLE.value, "the camera session ended (lease closed)"
            )

    async def _wait_ready(self, lease: _Lease, camera: str | None = None) -> bool:
        """Wait until the session is live and ``camera`` (default: the lease's first camera)
        delivers (its path is ready): True; False when the camera turned out silent (also when
        the source has not gone live within the camera settle time). Fails when the lease
        closed."""
        camera_id = camera or lease.camera_id
        attached = self.service.clock()
        while True:
            session = self.service.repository.get(lease.request.key)
            if session is None or lease.lease_id not in session.leases:
                raise FrameTapError(
                    RelayErrorCode.UNAVAILABLE.value, "the camera source did not start"
                )
            if session.state is SessionState.LIVE:
                available = session.camera_available(camera_id, self.service.clock())
                if available is not None:
                    return available
            elif (
                session.state is SessionState.STARTING
                and self.service.clock() - attached >= session.policy.camera_settle
            ):
                return False
            await asyncio.sleep(self._ready_poll)


@dataclass(slots=True)
class FrameStream:
    """An open frame stream: iterate :meth:`frames`, then :meth:`close`."""

    tap: FrameTap
    lease: _Lease
    fps: float
    max_width: int
    opened_at: float = field(init=False)
    _renewed_at: float = field(init=False)

    def __post_init__(self) -> None:
        self.opened_at = self.tap._monotonic()
        self._renewed_at = self.opened_at

    @property
    def camera_id(self) -> str:
        """The camera the stream shows."""
        return self.lease.camera_id

    @property
    def closed(self) -> bool:
        """The lease is detached."""
        return self.lease.closed

    async def frames(self) -> AsyncIterator[Frame]:
        """Frames until the hard cap, the end of the lease or the end of the path."""
        tap = self.tap
        deadline = self.opened_at + STREAM_HARD_CAP_SECONDS
        try:
            while not self.lease.closed:
                remaining = deadline - tap._monotonic()
                if remaining <= 0:
                    return
                reader = tap.grabber.stream(
                    self.lease.path, fps=self.fps, max_width=self.max_width, duration=remaining
                )
                try:
                    async with contextlib.aclosing(reader) as frames:
                        async for frame in frames:
                            if not await self._still_held():
                                return
                            yield frame
                except FrameUnavailableError:
                    if not tap._lease_open(self.lease):
                        return
                    await asyncio.sleep(tap._retry_wait)
                # The path ended (a source restart): read it again while the lease holds.
                if not tap._lease_open(self.lease):
                    return
        finally:
            await self.close()

    async def _still_held(self) -> bool:
        tap = self.tap
        now = tap._monotonic()
        if now >= self.opened_at + STREAM_HARD_CAP_SECONDS:
            return False
        if now - self._renewed_at >= STREAM_RENEW_SECONDS:
            self._renewed_at = now
            return await tap._renew(self.lease)
        return tap._lease_open(self.lease)

    async def close(self) -> None:
        """Detach the lease (idempotent, shielded)."""
        await self.tap._detach(self.lease)
