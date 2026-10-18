"""The frame tap: still frames and frame streams of a relay session for Vision (ADR 0045).

Vision (through the Device-Gateway) gets its camera frames from the relay instead of a headless
browser per capture (ADR 0035's worker). Every frame request holds a ``vision`` lease for
exactly its own lifetime:

* **Snapshot.** :meth:`FrameTap.snapshot` opens a lease (``VisionMode.SNAPSHOT``: the domain
  caps it at ``VISION_SNAPSHOT_HOLD_SECONDS``, 30 s, never extended), waits until the session is
  live, takes one frame of the camera's H.264 path and detaches, also when the caller gave up
  (timeout, cancellation): the detach is shielded.
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
import time
from collections.abc import AsyncGenerator, AsyncIterator, Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Final, Protocol
from uuid import UUID

from mowdeck_contracts.v1.principal import PrincipalRef
from mowdeck_contracts.v1.relay import (
    VISION_SNAPSHOT_HOLD_SECONDS,
    VISION_STREAM_LEASE_SECONDS,
    RelayAttachRequest,
    RelayDetachRequest,
    RelayErrorCode,
    RelayPurpose,
    RelayRenewRequest,
    VisionDemand,
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
    ) -> None:
        self.service = service
        self.grabber = grabber
        self._monotonic = monotonic
        self._ready_poll = ready_poll
        self._retry_wait = retry_wait
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
        self, request: FrameRequest, mode: VisionMode, *, avoid: list[str] | None = None
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
        camera = relay.tiles[0].camera_id
        return _Lease(
            request=request,
            lease_id=relay.lease_id,
            camera_id=camera,
            path=camera_path(session.relay_id, camera),
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
        """Wait for detaches still running (shutdown, tests)."""
        while self._detaching:
            await asyncio.gather(*list(self._detaching), return_exceptions=True)

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

    async def _wait_ready(self, lease: _Lease) -> bool:
        """Wait until the session is live and the lease's camera delivers (its path is ready):
        True; False when the camera turned out silent (also when the source has not gone live
        within the camera settle time with only this camera). Fails when the lease closed."""
        attached = self.service.clock()
        while True:
            session = self.service.repository.get(lease.request.key)
            if session is None or lease.lease_id not in session.leases:
                raise FrameTapError(
                    RelayErrorCode.UNAVAILABLE.value, "the camera source did not start"
                )
            if session.state is SessionState.LIVE:
                available = session.camera_available(lease.camera_id, self.service.clock())
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
