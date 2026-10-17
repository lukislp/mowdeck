"""The source pipeline: ffmpeg command lines, the fake source and the transcoder.

Paths on the media server (MediaMTX, RTSP inside the cluster only):

* ``r/<relay_id>/<camera>/src``: what a vendor source delivers (for Agora: H.265 from the
  bridge, :mod:`~camera_relay.adapters.media.bridge_source`);
* ``r/<relay_id>/<camera>``: the H.264 stream viewers read over WHEP.

The fake source (``CameraProvider.fake``, the simulator) publishes H.264 straight to the viewer
path. Every other source is transcoded: :func:`transcoder_args` reads ``.../src`` and writes
H.264 Constrained Baseline with a 1 s GOP to the viewer path. Two rules hold for every command
line (ADR 0045, "the relay never invents or repeats frames"):

* ``-fps_mode passthrough``: a frame in is at most one frame out; nothing is duplicated to fill
  a frame rate;
* no frame is generated while the input stalls: a frozen source is a frozen picture.

The fake source paces itself with the ``realtime`` filter and not with ``-re``: ``-re`` reads
ahead after a pause until it caught up with the wall clock (a burst of frames after a freeze),
while ``realtime=limit=...`` resets its clock after a pause and continues at 10 fps.
"""

import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from urllib.parse import quote
from uuid import UUID

from mowdeck_contracts.v1 import CameraSession
from mowdeck_contracts.v1.camera import CameraProvider
from mowdeck_service_kit import get_logger

from camera_relay.adapters.media.process import Spawn, SupervisedProcess, spawn_process
from camera_relay.domain.models import RelayKey
from camera_relay.domain.tickets import camera_path

_log = get_logger("camera_relay.media.pipeline")

FAKE_SIZE = "640x480"
FAKE_FPS = 10
GOP_FRAMES = 10
"""Key frame interval of every H.264 output: 1 s at the sources' 10 fps."""

H264_OUTPUT = (
    "-an",
    "-c:v",
    "libx264",
    "-preset",
    "ultrafast",
    "-tune",
    "zerolatency",
    "-profile:v",
    "baseline",
    "-pix_fmt",
    "yuv420p",
    "-bf",
    "0",
    "-g",
    str(GOP_FRAMES),
    "-fps_mode",
    "passthrough",
)
"""H.264 Constrained Baseline, no B-frames, zero latency, 1 s GOP, never a duplicated frame."""

_UNSAFE_LABEL = re.compile(r"[^A-Za-z0-9 _.-]")

COMMON = ("-hide_banner", "-nostdin", "-loglevel", "warning", "-progress", "pipe:1")


@dataclass(frozen=True, slots=True)
class MediaServer:
    """Where the pipeline publishes and with which credential."""

    rtsp_url: str
    """``rtsp://host:port`` without credentials."""
    user: str
    secret: Callable[[], str] = field(repr=False)

    def url(self, path: str) -> str:
        """The RTSP URL of ``path`` with the internal credential."""
        host = self.rtsp_url.removeprefix("rtsp://")
        user = quote(self.user, safe="")
        secret = quote(self.secret(), safe="")
        return f"rtsp://{user}:{secret}@{host}/{path}"

    def secrets(self) -> tuple[str, ...]:
        """What to redact in logs."""
        return (self.secret(), quote(self.secret(), safe=""))


def viewer_path(relay_id: str, camera_id: str) -> str:
    """``r/<relay_id>/<camera>``: the H.264 path viewers read."""
    return camera_path(relay_id, camera_id)


def source_path(relay_id: str, camera_id: str) -> str:
    """``r/<relay_id>/<camera>/src``: the vendor's stream before transcoding."""
    return f"{camera_path(relay_id, camera_id)}/src"


def _drawtext_literal(text: str) -> str:
    """``text`` reduced to characters that need no escaping in drawtext's ``text``."""
    return _UNSAFE_LABEL.sub("", text)[:80]


def fake_source_args(
    *,
    ffmpeg: str,
    output_url: str,
    mower_id: UUID,
    camera_id: str,
    font: str,
    size: str = FAKE_SIZE,
    fps: int = FAKE_FPS,
) -> list[str]:
    """A generated test pattern with an overlay (mower, camera, wall clock in ms) as H.264."""
    label = _drawtext_literal(f"{str(mower_id)[:8]}  {camera_id}")
    clock = "%{localtime\\:%X.%3N}"
    overlay = (
        f"drawtext=fontfile={font}:text='{label}  {clock}':x=12:y=h-th-12:fontsize=22"
        ":fontcolor=white:box=1:boxcolor=black@0.6:boxborderw=6"
    )
    source = f"testsrc2=size={size}:rate={fps},realtime=limit=0.5,{overlay}"
    return [
        ffmpeg,
        *COMMON,
        "-f",
        "lavfi",
        "-i",
        source,
        *H264_OUTPUT,
        "-f",
        "rtsp",
        "-rtsp_transport",
        "tcp",
        output_url,
    ]


def transcoder_args(*, ffmpeg: str, input_url: str, output_url: str) -> list[str]:
    """Read the source (H.265 or H.264), write H.264 Constrained Baseline, frame for frame."""
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
        *H264_OUTPUT,
        "-f",
        "rtsp",
        "-rtsp_transport",
        "tcp",
        output_url,
    ]


@dataclass(slots=True)
class RunningSource:
    """A started source of one relay session (what the drivers and the reconciler share)."""

    key: RelayKey
    session_id: UUID
    relay_id: str
    provider: CameraProvider
    cameras: tuple[str, ...]
    channel: str | None = None
    """The vendor session's channel (sent with the release)."""
    session: CameraSession | None = field(default=None, repr=False)
    """The vendor session the source started with (credentials: never logged)."""
    started_reported: bool = False
    """``source_started`` was reported to the use cases."""
    processes: dict[str, SupervisedProcess] = field(default_factory=dict)
    """Pipeline process per camera."""


class FakeSourceDriver:
    """``CameraProvider.fake``: one generated H.264 stream per camera, straight to the viewer
    path."""

    provider = CameraProvider.FAKE

    def __init__(
        self,
        *,
        server: MediaServer,
        ffmpeg: str,
        font: str,
        on_failure: Callable[[RunningSource], None],
        spawn: Spawn = spawn_process,
        max_restarts: int = 3,
    ) -> None:
        self.server = server
        self.ffmpeg = ffmpeg
        self.font = font
        self._on_failure = on_failure
        self._spawn = spawn
        self._max_restarts = max_restarts
        self.sources: dict[str, RunningSource] = {}

    def process_for(self, source: RunningSource, camera_id: str) -> SupervisedProcess:
        """The (not yet started) process of one camera."""
        args = fake_source_args(
            ffmpeg=self.ffmpeg,
            output_url=self.server.url(viewer_path(source.relay_id, camera_id)),
            mower_id=source.key.mower_id,
            camera_id=camera_id,
            font=self.font,
        )
        return SupervisedProcess(
            f"fake-{source.relay_id[:8]}-{camera_id}",
            args,
            secrets=self.server.secrets(),
            on_failure=lambda: self._on_failure(source),
            spawn=self._spawn,
            max_restarts=self._max_restarts,
        )

    async def available(self) -> bool:
        """The fake source needs nothing but ffmpeg."""
        return True

    async def start(self, source: RunningSource) -> None:
        """Start one process per camera."""
        self.sources[source.relay_id] = source
        await self._converge(source, source.cameras)

    async def update(self, source: RunningSource, cameras: Sequence[str]) -> None:
        """Start and stop processes to match ``cameras``."""
        await self._converge(source, tuple(cameras))

    async def stop(self, source: RunningSource) -> None:
        """Stop every process of the source."""
        await self._converge(source, ())
        self.sources.pop(source.relay_id, None)

    def freeze(self, frozen: bool, relay_id: str | None = None) -> int:
        """Pause (``frozen``) or resume the fake sources (all, or those of ``relay_id``).
        Returns how many processes changed."""
        changed = 0
        for source in list(self.sources.values()):
            if relay_id is not None and source.relay_id != relay_id:
                continue
            for process in source.processes.values():
                if process.pause() if frozen else process.resume():
                    changed += 1
        return changed

    async def _converge(self, source: RunningSource, cameras: tuple[str, ...]) -> None:
        for camera in [c for c in source.processes if c not in cameras]:
            await source.processes.pop(camera).stop()
        for camera in cameras:
            if camera not in source.processes:
                process = self.process_for(source, camera)
                source.processes[camera] = process
                await process.start()
        source.cameras = cameras


class Transcoder:
    """The generic transcoder of one camera: ``.../src`` (any codec ffmpeg reads) to the
    H.264 viewer path. Source drivers that deliver to ``.../src`` (the agora bridge source)
    start one per camera."""

    def __init__(
        self,
        *,
        server: MediaServer,
        ffmpeg: str,
        relay_id: str,
        camera_id: str,
        on_failure: Callable[[], None] | None = None,
        spawn: Spawn = spawn_process,
        max_restarts: int = 3,
    ) -> None:
        self.relay_id = relay_id
        self.camera_id = camera_id
        self.process = SupervisedProcess(
            f"transcode-{relay_id[:8]}-{camera_id}",
            transcoder_args(
                ffmpeg=ffmpeg,
                input_url=server.url(source_path(relay_id, camera_id)),
                output_url=server.url(viewer_path(relay_id, camera_id)),
            ),
            secrets=server.secrets(),
            on_failure=on_failure,
            spawn=spawn,
            max_restarts=max_restarts,
        )

    async def start(self) -> None:
        """Start transcoding."""
        await self.process.start()

    async def stop(self) -> None:
        """Stop transcoding."""
        await self.process.stop()
