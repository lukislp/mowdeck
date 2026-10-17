"""The ``agora`` source driver: a camera source bridge over TCP, one muxer per camera.

The relay image never contains a vendor SDK (ADR 0045). The Agora SDK runs in the optional
``agora-bridge`` container (``integrations/agora-bridge``, built by the operator) next to the
relay; its server listens on the relay's loopback (``MOWDECK_RELAY_AGORA_BRIDGE_URL``, e.g.
``tcp://127.0.0.1:7700``) and starts one bridge process per connection. The connection carries
the bridge protocol (docs/dev/camera-relay-bridge-protocol.md) unchanged.

Per source session (:class:`BridgeSourceDriver`):

* **Availability.** ``offer`` asks :meth:`BridgeSourceDriver.available` (a TCP connect that
  starts nothing). Without a configured or reachable bridge the relay keeps answering
  ``unsupported_provider``, so ``auto`` falls back to the direct path.
* **Start.** One connection (a :class:`BridgeLink`): ``join`` with the vendor session (secrets
  only in that line, never logged), ``subscribe`` with the uids of the leased cameras. Cameras
  are optional: a camera whose uid never publishes simply never gets media.
* **Frames.** Per camera a :class:`CameraFeed`: from the first key frame on, every access unit
  goes into an MPEG transport stream (:mod:`~camera_relay.adapters.media.mpegts`) with its
  presentation time from the bridge's ``recv_mono_ns``, piped into an ffmpeg that publishes
  ``r/<rid>/<cam>/src`` (H.265, ``-c copy``); the generic :class:`Transcoder` turns it into the
  H.264 viewer path (1 s GOP, passthrough). Nothing is repeated or invented; a full pipe drops
  delta frames until the next key frame.
* **Renewal.** Vendor sessions last about an hour. ``renew_lead`` (300 s) before
  ``expires_at`` the driver asks the gateway for a fresh session: same channel, ``renew`` on the
  running connection; ``renew_failed``, no answer, another channel, or the bridge's
  ``token_will_expire`` afterwards: a **rejoin**, make before break: a second connection joins
  with the fresh session, each camera switches to it at its first key frame, then the old one
  leaves. Viewers see at most one GOP gap.
* **Failure.** The connection of the active bridge ends without ``leave`` (crash, exit record
  with a code, protocol error): the source fails (``source_failed``: ``FAILED`` and the backoff
  of the session domain).
* **Stop.** ``leave`` on every connection, then the muxers and transcoders stop; the gateway's
  release runs afterwards (``GatewayVendorSessions.stop``).
"""

import asyncio
import contextlib
import time
from collections import deque
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol
from urllib.parse import urlsplit

from mowdeck_contracts.v1 import CameraSession, utc_now
from mowdeck_contracts.v1.camera import CameraProvider
from mowdeck_service_kit import get_logger

from camera_relay.adapters.media.bridge_protocol import (
    CODEC_H264,
    CODEC_H265,
    BridgeFrame,
    BridgeProtocolError,
    BridgeRecord,
    RecordReader,
    control_line,
    join_line,
    renew_line,
)
from camera_relay.adapters.media.mpegts import CLOCK, TsWriter
from camera_relay.adapters.media.pipeline import (
    COMMON,
    MediaServer,
    RunningSource,
    Transcoder,
    source_path,
)
from camera_relay.adapters.media.process import Spawn, redact, spawn_process
from camera_relay.domain.models import RelayKey

_log = get_logger("camera_relay.media.bridge")

CONNECT_TIMEOUT_SECONDS = 3.0
PROBE_TIMEOUT_SECONDS = 1.0
RENEW_LEAD_SECONDS = 300.0
"""Renew this long before the vendor session expires (ADR 0045: about five minutes)."""
RENEW_REPLY_SECONDS = 10.0
RETRY_SECONDS = 30.0
SWITCH_TIMEOUT_SECONDS = 12.0
"""A rejoin finishes at the latest this long after the new connection was opened (a camera
without a key frame by then switches at its next one)."""
LEAVE_TIMEOUT_SECONDS = 3.0
MUXER_BUFFER_LIMIT = 2 * 1024 * 1024
"""Bytes waiting in a muxer's stdin before delta frames are dropped until the next key frame."""
FRAME_INTERVAL_NS = 100_000_000
"""Nominal frame interval (10 fps) for the first frame of a stream and after a switch."""


class StdinProcess(Protocol):
    """What a muxer needs of ``asyncio.subprocess.Process``."""

    @property
    def pid(self) -> int:
        """The process id."""
        ...

    @property
    def returncode(self) -> int | None:
        """The exit code, None while running."""
        ...

    @property
    def stdin(self) -> asyncio.StreamWriter | None:
        """Where the transport stream goes."""
        ...

    @property
    def stderr(self) -> asyncio.StreamReader | None:
        """ffmpeg's log."""
        ...

    async def wait(self) -> int:
        """Wait for the exit."""
        ...

    def kill(self) -> None:
        """End the process."""
        ...


class StdinSpawn(Protocol):
    """Starts a process with a stdin pipe."""

    async def __call__(self, *args: str) -> StdinProcess:
        """Start ``args``."""
        ...


async def spawn_stdin_process(*args: str) -> StdinProcess:
    """Start ``args`` with stdin and stderr piped (stdout discarded)."""
    process = await asyncio.create_subprocess_exec(
        *args,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )
    return process


def muxer_args(*, ffmpeg: str, output_url: str) -> list[str]:
    """MPEG-TS on stdin to RTSP, stream copy. ``-fflags nobuffer`` must not be used: it drops
    the packets ffmpeg read while probing, i.e. the first key frame."""
    return [
        ffmpeg,
        *COMMON[:4],  # -hide_banner -nostdin -loglevel warning
        "-probesize",
        "32768",
        "-analyzeduration",
        "0",
        "-flags",
        "low_delay",
        "-f",
        "mpegts",
        "-i",
        "pipe:0",
        "-an",
        "-c:v",
        "copy",
        "-f",
        "rtsp",
        "-rtsp_transport",
        "tcp",
        output_url,
    ]


@dataclass(frozen=True, slots=True)
class BridgeEndpoint:
    """Where the bridge server listens (``tcp://host:port``)."""

    host: str
    port: int

    @classmethod
    def parse(cls, url: str) -> "BridgeEndpoint":
        """Parse ``tcp://host:port``; raises ``ValueError``."""
        parts = urlsplit(url)
        if parts.scheme != "tcp" or not parts.hostname or parts.port is None:
            msg = "the bridge URL must be tcp://host:port"
            raise ValueError(msg)
        return cls(parts.hostname, parts.port)

    def __str__(self) -> str:
        return f"tcp://{self.host}:{self.port}"


class BridgeLink:
    """One connection to the bridge server: one bridge process, one channel join."""

    _ids = 0

    def __init__(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        uid_cameras: dict[int, str],
    ) -> None:
        BridgeLink._ids += 1
        self.id = BridgeLink._ids
        self._reader = reader
        self._writer = writer
        self.uid_cameras = uid_cameras
        self.exit_code: int | None = None
        self.left = False
        self.task: asyncio.Task[None] | None = None

    @classmethod
    async def open(
        cls, endpoint: BridgeEndpoint, uid_cameras: dict[int, str], connect_seconds: float
    ) -> "BridgeLink":
        """Connect; raises ``OSError`` or ``TimeoutError``."""
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(endpoint.host, endpoint.port), connect_seconds
        )
        return cls(reader, writer, uid_cameras)

    async def send(self, line: bytes) -> None:
        """Write one control line (may carry secrets: never logged); raises
        ``ConnectionError`` when the connection is gone."""
        if self._writer.is_closing():
            msg = "the bridge connection is closed"
            raise ConnectionResetError(msg)
        try:
            self._writer.write(line)
            await self._writer.drain()
        except RuntimeError as exc:  # uvloop: write on a transport that just closed
            msg = "the bridge connection is closed"
            raise ConnectionResetError(msg) from exc

    async def records(self) -> AsyncIterator[BridgeRecord]:
        """The bridge's records until the connection ends."""
        parser = RecordReader()
        while chunk := await self._reader.read(256 * 1024):
            for record in parser.feed(chunk):
                yield record

    async def close(self) -> None:
        """Close the connection (the bridge's stdin closes: it leaves)."""
        with contextlib.suppress(OSError, ConnectionError):
            self._writer.close()
            await self._writer.wait_closed()


class CameraFeed:
    """One camera: the muxer (MPEG-TS on stdin -> ``.../src``) and the transcoder."""

    def __init__(
        self,
        *,
        camera_id: str,
        relay_id: str,
        server: MediaServer,
        ffmpeg: str,
        on_failure: Callable[[], None],
        spawn: Spawn,
        spawn_stdin: StdinSpawn,
        transcoder_delay: float = 1.0,
        max_restarts: int = 3,
        restart_window: float = 60.0,
    ) -> None:
        self.camera_id = camera_id
        self.relay_id = relay_id
        self._server = server
        self._ffmpeg = ffmpeg
        self._on_failure = on_failure
        self._spawn = spawn
        self._spawn_stdin = spawn_stdin
        self._transcoder_delay = transcoder_delay
        self._max_restarts = max_restarts
        self._restart_window = restart_window
        self.link: BridgeLink | None = None
        """The connection whose frames this camera takes."""
        self.process: StdinProcess | None = None
        self.writer: TsWriter | None = None
        self.transcoder: Transcoder | None = None
        self._transcoder_task: asyncio.Task[None] | None = None
        self._watch: asyncio.Task[None] | None = None
        self._restarts: deque[float] = deque()
        self._awaiting_key = True
        self._pts: int | None = None
        self._last_recv: int | None = None
        self._last_link: int | None = None
        self.stopping = False
        self.failed = False
        self._lock = asyncio.Lock()
        self.frames_written = 0
        self.frames_dropped = 0
        self.starts = 0
        self.last_error = ""

    async def push(self, frame: BridgeFrame, link: BridgeLink) -> None:
        """Write one frame from ``link`` (from a key frame on). Serialized per camera: during a
        rejoin two connections' readers may push, and a muxer start must not race."""
        async with self._lock:
            await self._push(frame, link)

    async def _push(self, frame: BridgeFrame, link: BridgeLink) -> None:
        if self.stopping or self.failed or frame.codec not in (CODEC_H264, CODEC_H265):
            return
        alive = self.process is not None and self.process.returncode is None
        if alive and self.writer is not None and frame.codec != self.writer.codec:
            if not frame.keyframe:
                self.frames_dropped += 1
                return
            await self._stop_muxer()  # the codec changed (another source): a new stream
            alive = False
        if not frame.keyframe and (not alive or self._awaiting_key):
            self.frames_dropped += 1
            return
        if alive and not self._writable():
            self.frames_dropped += 1
            return
        pts = self._next_pts(frame, link)
        if not alive and not await self._start_muxer(frame.codec, pts):
            return
        assert self.process is not None and self.writer is not None  # noqa: S101 - started
        stdin = self.process.stdin
        if stdin is None or stdin.is_closing():
            self.frames_dropped += 1
            return
        stdin.write(self.writer.frame(frame.data, pts, keyframe=frame.keyframe))
        self._awaiting_key = False
        self.frames_written += 1
        if self.transcoder is None and self._transcoder_task is None:
            self._transcoder_task = asyncio.create_task(
                self._start_transcoder(), name=f"camera-relay-transcoder-{self.camera_id}"
            )

    def _writable(self) -> bool:
        """False (and from now on waiting for a key frame) while ffmpeg does not keep up."""
        stdin = self.process.stdin if self.process is not None else None
        if stdin is None or stdin.transport.get_write_buffer_size() <= MUXER_BUFFER_LIMIT:
            return True
        if not self._awaiting_key:
            _log.warning(
                "muxer falls behind: dropping until the next key frame", camera=self.camera_id
            )
        self._awaiting_key = True
        return False

    def _next_pts(self, frame: BridgeFrame, link: BridgeLink) -> int:
        """90 kHz time from the bridge's receive clock; monotonic across restarts and links."""
        if self._pts is None or self._last_recv is None:
            pts = CLOCK  # 1 s: room for the lead delimiter
        else:
            if link.id == self._last_link:
                delta_ns = frame.recv_mono_ns - self._last_recv
            else:
                delta_ns = FRAME_INTERVAL_NS  # another bridge process: another clock base
            delta = max(1, delta_ns * CLOCK // 1_000_000_000)
            pts = self._pts + delta
        self._pts, self._last_recv, self._last_link = pts, frame.recv_mono_ns, link.id
        return pts

    async def _start_muxer(self, codec: int, pts: int) -> bool:
        if self.starts and not self._may_restart():
            self.failed = True
            _log.error("muxer failed for good", camera=self.camera_id, error=self.last_error)
            self._on_failure()
            return False
        args = muxer_args(
            ffmpeg=self._ffmpeg,
            output_url=self._server.url(source_path(self.relay_id, self.camera_id)),
        )
        try:
            process = await self._spawn_stdin(*args)
        except OSError:
            _log.exception("muxer could not be started", camera=self.camera_id)
            self.failed = True
            self._on_failure()
            return False
        self.starts += 1
        self.process = process
        self.writer = TsWriter(codec)
        self._awaiting_key = True
        if process.stdin is not None:
            process.stdin.write(self.writer.start(pts))
        self._watch = asyncio.create_task(
            self._supervise(process), name=f"camera-relay-muxer-{self.camera_id}"
        )
        _log.info("muxer started", camera=self.camera_id, pid=process.pid, codec=codec)
        return True

    def _may_restart(self) -> bool:
        now = time.monotonic()
        while self._restarts and now - self._restarts[0] > self._restart_window:
            self._restarts.popleft()
        if len(self._restarts) >= self._max_restarts:
            return False
        self._restarts.append(now)
        return True

    async def _supervise(self, process: StdinProcess) -> None:
        secrets = self._server.secrets()
        if process.stderr is not None:
            while line := await process.stderr.readline():
                text = redact(line.decode("utf-8", "replace").rstrip(), secrets)
                if text:
                    self.last_error = text
                    _log.debug("muxer", camera=self.camera_id, line=text[:500])
        code = await process.wait()
        if not self.stopping:
            _log.warning(
                "muxer exited; restarting at the next key frame",
                camera=self.camera_id,
                code=code,
                error=self.last_error[-300:],
            )

    async def _start_transcoder(self) -> None:
        await asyncio.sleep(self._transcoder_delay)  # the muxer publishes .../src first
        if self.stopping or self.failed:
            return
        transcoder = Transcoder(
            server=self._server,
            ffmpeg=self._ffmpeg,
            relay_id=self.relay_id,
            camera_id=self.camera_id,
            on_failure=self._on_failure,
            spawn=self._spawn,
        )
        self.transcoder = transcoder
        await transcoder.start()

    async def _stop_muxer(self) -> None:
        process, watch = self.process, self._watch
        self.process, self.writer, self._watch = None, None, None
        if process is None:
            return
        if process.stdin is not None:
            with contextlib.suppress(OSError, ConnectionError):
                process.stdin.close()
        try:
            await asyncio.wait_for(process.wait(), 5.0)
        except TimeoutError:
            with contextlib.suppress(ProcessLookupError):
                process.kill()
            await process.wait()
        if watch is not None:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await asyncio.wait_for(watch, 1.0)

    async def stop(self) -> None:
        """Stop the muxer and the transcoder. Idempotent."""
        self.stopping = True
        if self._transcoder_task is not None:
            self._transcoder_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._transcoder_task
        await self._stop_muxer()
        if self.transcoder is not None:
            await self.transcoder.stop()


@dataclass(slots=True)
class _Run:
    """The driver's state of one source session."""

    source: RunningSource
    session: CameraSession = field(repr=False)
    feeds: dict[str, CameraFeed] = field(default_factory=dict)
    active: BridgeLink | None = None
    pending: BridgeLink | None = None
    renewal: asyncio.Task[None] | None = None
    renew_reply: asyncio.Future[str] | None = None
    rejoining: bool = False
    stopping: bool = False
    failed: bool = False
    tasks: set[asyncio.Task[Any]] = field(default_factory=set)


class BridgeSourceDriver:
    """``CameraProvider.agora`` through the agora-bridge server."""

    provider = CameraProvider.AGORA

    def __init__(
        self,
        *,
        endpoint: BridgeEndpoint,
        server: MediaServer,
        ffmpeg: str,
        on_failure: Callable[[RunningSource], None],
        fetch_session: Callable[[RelayKey], Awaitable[CameraSession]],
        spawn: Spawn = spawn_process,
        spawn_stdin: StdinSpawn = spawn_stdin_process,
        now: Callable[[], datetime] = utc_now,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        renew_lead: float = RENEW_LEAD_SECONDS,
        connect_timeout: float = CONNECT_TIMEOUT_SECONDS,
        switch_timeout: float = SWITCH_TIMEOUT_SECONDS,
        transcoder_delay: float = 1.0,
    ) -> None:
        self.endpoint = endpoint
        self.server = server
        self.ffmpeg = ffmpeg
        self._on_failure = on_failure
        self._fetch_session = fetch_session
        self._spawn = spawn
        self._spawn_stdin = spawn_stdin
        self._now = now
        self._sleep = sleep
        self.renew_lead = renew_lead
        self.connect_timeout = connect_timeout
        self.switch_timeout = switch_timeout
        self.transcoder_delay = transcoder_delay
        self.runs: dict[str, _Run] = {}

    # --- SourceDriver -------------------------------------------------------------------------

    async def available(self) -> bool:
        """The bridge server accepts connections (a connect that starts no bridge)."""
        try:
            _, writer = await asyncio.wait_for(
                asyncio.open_connection(self.endpoint.host, self.endpoint.port),
                PROBE_TIMEOUT_SECONDS,
            )
        except (OSError, TimeoutError):
            _log.info("agora bridge not reachable", endpoint=str(self.endpoint))
            return False
        with contextlib.suppress(OSError, ConnectionError):
            writer.close()
            await writer.wait_closed()
        return True

    async def start(self, source: RunningSource) -> None:
        """Join with the source's vendor session and subscribe its cameras."""
        session = source.session or await self._fetch_session(source.key)
        run = _Run(source=source, session=session)
        self.runs[source.relay_id] = run
        try:
            link = await self._open(run, session)
        except Exception:
            self.runs.pop(source.relay_id, None)
            raise
        run.active = link
        for camera in source.cameras:
            run.feeds[camera] = self._feed(run, camera)
        try:
            await link.send(self._subscribe_line(link, source.cameras))
        except BaseException:
            await self.stop(source)  # nothing half-running: the gateway releases and fails
            raise
        run.renewal = self._task(run, self._renewal(run), "renewal")
        _log.info(
            "agora source started",
            mower_id=str(source.key.mower_id),
            cameras=list(source.cameras),
            link=link.id,
        )

    async def update(self, source: RunningSource, cameras: Sequence[str]) -> None:
        """Publish exactly ``cameras``."""
        run = self.runs.get(source.relay_id)
        if run is None:
            return
        wanted = tuple(cameras)
        for camera in [c for c in run.feeds if c not in wanted]:
            await run.feeds.pop(camera).stop()
        for camera in wanted:
            if camera not in run.feeds:
                run.feeds[camera] = self._feed(run, camera)
        source.cameras = wanted
        for link in (run.active, run.pending):
            if link is not None:
                with contextlib.suppress(OSError, ConnectionError):
                    await link.send(self._subscribe_line(link, wanted))

    async def stop(self, source: RunningSource) -> None:
        """Leave the channel and stop every camera."""
        run = self.runs.pop(source.relay_id, None)
        if run is None:
            return
        run.stopping = True
        for task in list(run.tasks):
            if task is not asyncio.current_task() and not _is_reader(task):
                task.cancel()
        try:
            for link in (run.pending, run.active):
                if link is not None:
                    try:
                        await self._leave(link)
                    except Exception:
                        _log.exception("leaving the agora channel failed", link=link.id)
        finally:
            for feed in run.feeds.values():
                try:
                    await feed.stop()
                except Exception:
                    _log.exception("camera pipeline not stopped cleanly", camera=feed.camera_id)
        children = [task for task in list(run.tasks) if task is not asyncio.current_task()]
        for task in children:
            task.cancel()
        await asyncio.gather(*children, return_exceptions=True)
        _log.info("agora source stopped", mower_id=str(source.key.mower_id))

    # --- Links --------------------------------------------------------------------------------

    @staticmethod
    def _uid_cameras(session: CameraSession) -> dict[int, str]:
        uids: dict[int, str] = {}
        for camera in session.cameras:
            if camera.uid is None:
                _log.warning("camera without a publisher uid is skipped", camera=camera.id)
                continue
            uids[camera.uid] = camera.id
        if not uids:
            msg = "the vendor session names no camera uid"
            raise ValueError(msg)
        return uids

    def _subscribe_line(self, link: BridgeLink, cameras: Sequence[str]) -> bytes:
        uids = sorted(uid for uid, camera in link.uid_cameras.items() if camera in cameras)
        return control_line("subscribe", uids=uids)

    async def _open(self, run: _Run, session: CameraSession) -> BridgeLink:
        link = await BridgeLink.open(
            self.endpoint, self._uid_cameras(session), self.connect_timeout
        )
        try:
            await link.send(join_line(session))
        except (OSError, ConnectionError):
            await link.close()
            raise
        link.task = self._task(run, self._read(run, link), f"reader-{link.id}")
        return link

    async def _leave(self, link: BridgeLink) -> None:
        link.left = True
        with contextlib.suppress(OSError, ConnectionError):
            await link.send(control_line("leave"))  # a crashed bridge needs no leave
        if link.task is not None and not link.task.done():
            # Only the reader's own outcome is suppressed, never a cancel of the caller.
            with contextlib.suppress(TimeoutError, Exception):
                await asyncio.wait_for(asyncio.shield(link.task), LEAVE_TIMEOUT_SECONDS)
        await link.close()

    async def _read(self, run: _Run, link: BridgeLink) -> None:
        reason = "closed"
        try:
            async for record in link.records():
                if isinstance(record, BridgeFrame):
                    await self._frame(run, link, record)
                else:
                    await self._event(run, link, record)
        except BridgeProtocolError as exc:
            reason = f"protocol error: {exc}"
            _log.error("agora bridge broke the protocol", link=link.id, error=str(exc))
        except (OSError, ConnectionError) as exc:
            reason = type(exc).__name__
        finally:
            await link.close()
        await self._ended(run, link, reason)

    async def _ended(self, run: _Run, link: BridgeLink, reason: str) -> None:
        if run.stopping or link.left:
            return
        _log.warning(
            "agora bridge connection ended", link=link.id, code=link.exit_code, reason=reason
        )
        if link is run.pending:
            run.pending = None
            run.rejoining = False
            return
        if link is not run.active:
            return
        if run.pending is not None:
            # The old connection died during a rejoin: the new one takes every camera.
            self._finish_switch(run, force=True)
            return
        self._fail(run)

    def _fail(self, run: _Run) -> None:
        if run.failed or run.stopping:
            return
        run.failed = True
        self._on_failure(run.source)

    # --- Records ------------------------------------------------------------------------------

    async def _frame(self, run: _Run, link: BridgeLink, frame: BridgeFrame) -> None:
        camera = link.uid_cameras.get(frame.uid)
        feed = run.feeds.get(camera) if camera is not None else None
        if feed is None:
            return
        if feed.link is None and link is run.active:
            feed.link = link
        elif feed.link is not link:
            if link is not run.pending or not frame.keyframe:
                return
            feed.link = link  # this camera switches to the new connection at a key frame
            await feed.push(frame, link)
            self._finish_switch(run)
            return
        await feed.push(frame, link)

    async def _event(self, run: _Run, link: BridgeLink, event: dict[str, Any]) -> None:
        name = event.get("event")
        match name:
            case "hello":
                _log.info("agora bridge", link=link.id, sdk=str(event.get("sdk"))[:80])
            case "joined":
                _log.info("agora channel joined", link=link.id, elapsed_ms=event.get("elapsed_ms"))
            case "publisher":
                _log.info(
                    "agora publisher", link=link.id, uid=event.get("uid"), state=event.get("state")
                )
            case "video_state":
                _log.debug("agora video state", link=link.id, **_plain(event))
            case "token_will_expire":
                if link is run.active and not run.rejoining:
                    _log.info("agora token expires soon: rejoining", link=link.id)
                    self._task(run, self._rejoin_logged(run), "rejoin")
            case "renewed" | "renew_failed":
                if run.renew_reply is not None and not run.renew_reply.done():
                    run.renew_reply.set_result(name)
            case "error":
                _log.warning("agora bridge error", link=link.id, **_plain(event))
            case "exit":
                code = event.get("code")
                link.exit_code = code if isinstance(code, int) else -1
            case "left":
                _log.info("agora channel left", link=link.id)
            case _:
                _log.debug("agora bridge event", link=link.id, event_name=str(name)[:32])

    # --- Renewal and rejoin -------------------------------------------------------------------

    def renew_delay(self, session: CameraSession) -> float:
        """Seconds until the next renewal of ``session``."""
        remaining = (session.expires_at - self._now()).total_seconds()
        lead = min(self.renew_lead, max(remaining, 0) / 2)
        return max(remaining - lead, 5.0)

    async def _renewal(self, run: _Run) -> None:
        while not run.stopping:
            await self._sleep(self.renew_delay(run.session))
            if run.stopping:
                return
            try:
                await self._refresh(run)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                _log.warning("agora renewal failed; retrying", error=type(exc).__name__)
                await self._sleep(RETRY_SECONDS)

    async def _refresh(self, run: _Run) -> None:
        if run.rejoining or run.pending is not None:
            await self._sleep(RETRY_SECONDS)  # a rejoin is under way: its session is fresh
            return
        fresh = await self._fetch_session(run.source.key)
        link = run.active
        same = (
            link is not None
            and fresh.channel == run.session.channel
            and fresh.app_id == run.session.app_id
        )
        if same and link is not None:
            loop = asyncio.get_running_loop()
            run.renew_reply = loop.create_future()
            try:
                await link.send(renew_line(fresh))
                outcome = await asyncio.wait_for(run.renew_reply, RENEW_REPLY_SECONDS)
            except TimeoutError:
                outcome = "timeout"
            except (OSError, ConnectionError):
                outcome = "send_failed"
            finally:
                run.renew_reply = None
            if outcome == "renewed":
                run.session = fresh
                _log.info("agora token renewed", link=link.id, expires_at=fresh.expires_at)
                return
            _log.warning("agora token renewal refused: rejoining", outcome=outcome)
        await self._rejoin(run, fresh)

    async def _rejoin(self, run: _Run, session: CameraSession | None = None) -> None:
        if run.rejoining or run.pending is not None or run.stopping:
            return
        run.rejoining = True
        try:
            fresh = session or await self._fetch_session(run.source.key)
            link = await self._open(run, fresh)
        except Exception as exc:
            run.rejoining = False
            _log.warning("agora rejoin failed", error=type(exc).__name__)
            raise
        run.pending = link
        run.session = fresh
        await link.send(self._subscribe_line(link, run.source.cameras))
        _log.info("agora rejoin: new connection", link=link.id)
        self._task(run, self._switch_deadline(run, link), f"switch-{link.id}")

    async def _rejoin_logged(self, run: _Run) -> None:
        """A rejoin the renewal loop does not wait for (its failure is logged by ``_rejoin``;
        the renewal retries)."""
        with contextlib.suppress(Exception):
            await self._rejoin(run)

    async def _switch_deadline(self, run: _Run, link: BridgeLink) -> None:
        await asyncio.sleep(self.switch_timeout)
        if run.pending is link:
            self._finish_switch(run, force=True)

    def _finish_switch(self, run: _Run, *, force: bool = False) -> None:
        new = run.pending
        if new is None:
            return
        old = run.active
        # Done when no camera takes frames from the old connection any more (a camera that
        # never published, like the LUBA's rear one, does not hold the switch up).
        if not force and any(feed.link is old for feed in run.feeds.values()):
            return
        for feed in run.feeds.values():
            if feed.link is not new:
                feed.link = new  # waits for its next key frame from the new connection
        run.active, run.pending, run.rejoining = new, None, False
        _log.info("agora rejoin complete", link=new.id, previous=old.id if old else None)
        if old is not None:
            self._task(run, self._leave(old), f"leave-{old.id}")

    # --- Helpers ------------------------------------------------------------------------------

    def _feed(self, run: _Run, camera: str) -> CameraFeed:
        return CameraFeed(
            camera_id=camera,
            relay_id=run.source.relay_id,
            server=self.server,
            ffmpeg=self.ffmpeg,
            on_failure=lambda: self._fail(run),
            spawn=self._spawn,
            spawn_stdin=self._spawn_stdin,
            transcoder_delay=self.transcoder_delay,
        )

    def _task(self, run: _Run, coroutine: Awaitable[Any], name: str) -> asyncio.Task[Any]:
        task = asyncio.ensure_future(coroutine)
        task.set_name(f"camera-relay-bridge-{name}")
        run.tasks.add(task)
        task.add_done_callback(run.tasks.discard)
        return task


def _is_reader(task: asyncio.Task[Any]) -> bool:
    return task.get_name().startswith("camera-relay-bridge-reader-")


def _plain(event: dict[str, Any]) -> dict[str, Any]:
    """An event's scalar fields for a log line (events never carry secrets)."""
    return {
        f"bridge_{key}": (value if isinstance(value, int | float | bool) else str(value)[:200])
        for key, value in event.items()
        if key != "event"
    }
