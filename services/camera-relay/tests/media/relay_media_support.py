"""The media test stack (compose.yml) and a fake Device-Gateway on its NATS.

Runs only with ``MOWDECK_TEST_RELAY_MEDIA=1`` and a built relay image
(``RELAY_TEST_IMAGE``, default ``mowdeck/camera-relay:dev``):

    docker build -f services/camera-relay/Dockerfile -t mowdeck/camera-relay:dev .
    MOWDECK_TEST_RELAY_MEDIA=1 uv run pytest services/camera-relay/tests/media

The stack is a Compose project of its own (``camera-relay-media-test``), removed afterwards
(``RELAY_TEST_KEEP=1`` keeps it).

The H.265 stream the tests replay is synthetic: :func:`synthetic_h265` encodes it once with the
relay image's own ffmpeg (libx265) into ``.fixtures/`` (ignored by git) before the stack starts.
It has the structure of the LUBA 2 AWD X front camera (640x480, 10 fps, 271 frames, P frames
only, an IDR with VPS/SPS/PPS every 40 frames, the 4 s GOP) and no recorded picture.
"""

import asyncio
import json
import os
import subprocess
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path
from typing import Any
from uuid import UUID

import httpx
import nats
import pytest
from mowdeck_contracts.v1 import (
    CameraReleaseReply,
    CameraReleaseRequest,
    CameraSession,
    CameraSessionReply,
    CameraSessionRequest,
    utc_now,
)
from mowdeck_contracts.v1.camera import CameraInfo, CameraPlacement, CameraProvider
from mowdeck_contracts.v1.subjects import ALL_CAMERA_RELEASES, ALL_CAMERA_SESSIONS
from nats.aio.client import Client
from nats.aio.msg import Msg

HERE = Path(__file__).parent
COMPOSE = HERE / "compose.yml"
PROJECT = "camera-relay-media-test"
SECRET = "media-test-publisher-secret"  # noqa: S105 - the test stack's fixed secret
FIXTURES = HERE / ".fixtures"
H265_FIXTURE = FIXTURES / "camera-h265.h265"
"""Mounted at ``/opt/fixtures`` in the stack (compose.yml)."""
H265_FRAMES = 271
H265_GOP = 40


def _port(name: str, default: int) -> int:
    return int(os.environ.get(name, str(default)))


@dataclass(frozen=True, slots=True)
class MediaStack:
    """Where the test stack listens."""

    nats_url: str
    relay_url: str
    whep_url: str
    api_url: str

    def compose(self, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        """Run ``docker compose`` against the stack."""
        return _compose(*args, check=check)


def _compose(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603
        ["docker", "compose", "-p", PROJECT, "-f", str(COMPOSE), *args],  # noqa: S607
        capture_output=True,
        text=True,
        check=check,
        timeout=600,
    )


def synthetic_h265(target: Path = H265_FIXTURE) -> Path:
    """Encode the synthetic H.265 test stream with the relay image's ffmpeg (once).

    ffmpeg writes Annex-B to stdout, so the container needs no writable mount: a moving test
    pattern (``testsrc2``), Main profile, no B frames, a closed GOP of ``H265_GOP`` frames
    without scene cuts and the parameter sets repeated before every key frame, like the mower.
    """
    if target.is_file() and target.stat().st_size > 0:
        return target
    image = os.environ.get("RELAY_TEST_IMAGE", "mowdeck/camera-relay:dev")
    x265 = f"log-level=error:keyint={H265_GOP}:min-keyint={H265_GOP}:scenecut=0:bframes=0"
    encoded = subprocess.run(  # noqa: S603
        [  # noqa: S607
            "docker",
            "run",
            "--rm",
            "--network",
            "none",
            image,
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=640x480:rate=10",
            "-frames:v",
            str(H265_FRAMES),
            "-c:v",
            "libx265",
            "-preset",
            "ultrafast",
            "-pix_fmt",
            "yuv420p",
            "-x265-params",
            f"{x265}:open-gop=0:repeat-headers=1",
            "-f",
            "hevc",
            "-",
        ],
        capture_output=True,
        check=False,
        timeout=300,
    )
    if encoded.returncode != 0 or not encoded.stdout:
        pytest.fail(f"cannot encode the synthetic H.265 stream: {encoded.stderr.decode()[-2000:]}")
    target.parent.mkdir(mode=0o755, exist_ok=True)
    target.parent.chmod(0o755)  # the containers read it as uid 10001
    partial = target.with_suffix(".partial")
    partial.write_bytes(encoded.stdout)
    partial.chmod(0o644)
    partial.replace(target)
    return target


@pytest.fixture(scope="session")
def media_stack() -> Iterator[MediaStack]:
    if os.environ.get("MOWDECK_TEST_RELAY_MEDIA") != "1":
        pytest.skip("set MOWDECK_TEST_RELAY_MEDIA=1 (needs Docker and the relay image)")
    synthetic_h265()
    _compose("down", "-v", "--remove-orphans", check=False)
    started = _compose("up", "-d", "--wait", "--wait-timeout", "120", check=False)
    if started.returncode != 0:
        logs = _compose("logs", "--no-color", check=False).stdout
        _compose("down", "-v", check=False)
        pytest.fail(f"media test stack did not start:\n{started.stderr}\n{logs[-4000:]}")
    try:
        yield MediaStack(
            nats_url=f"nats://127.0.0.1:{_port('RELAY_TEST_NATS_PORT', 14322)}",
            relay_url=f"http://127.0.0.1:{_port('RELAY_TEST_HTTP_PORT', 18321)}",
            whep_url=f"http://127.0.0.1:{_port('RELAY_TEST_WHEP_PORT', 18889)}",
            api_url=f"http://127.0.0.1:{_port('RELAY_TEST_API_PORT', 18997)}",
        )
    finally:
        if os.environ.get("RELAY_TEST_LOGS"):
            Path(os.environ["RELAY_TEST_LOGS"]).write_text(
                _compose("logs", "--no-color", "--timestamps", check=False).stdout, "utf-8"
            )
        if os.environ.get("RELAY_TEST_KEEP") != "1":
            _compose("down", "-v", "--remove-orphans", check=False)


@dataclass(slots=True)
class FakeGateway:
    """Answers ``gateway.mower.*.camera.session|release`` like the gateway with a simulated
    mower (provider ``fake``, two front cameras) and records every request."""

    sessions: list[CameraSessionRequest] = field(default_factory=list)
    releases: list[CameraReleaseRequest] = field(default_factory=list)
    provider: CameraProvider = CameraProvider.FAKE

    def session_for(self, mower_id: UUID) -> CameraSession:
        return CameraSession(
            provider=self.provider,
            app_id="",
            channel=f"sim-{str(mower_id)[:8]}",
            token="",
            uid=4242,
            cameras=[
                CameraInfo(
                    id="front_left", name="Front left", uid=1, placement=CameraPlacement.FRONT_LEFT
                ),
                CameraInfo(
                    id="front_right",
                    name="Front right",
                    uid=2,
                    placement=CameraPlacement.FRONT_RIGHT,
                ),
            ],
            expires_at=utc_now() + timedelta(minutes=5),
        )

    async def on_session(self, msg: Msg) -> None:
        request = CameraSessionRequest.model_validate_json(msg.data)
        self.sessions.append(request)
        reply = CameraSessionReply(session=self.session_for(request.mower_id))
        await msg.respond(reply.model_dump_json().encode())

    async def on_release(self, msg: Msg) -> None:
        request = CameraReleaseRequest.model_validate_json(msg.data)
        self.releases.append(request)
        await msg.respond(CameraReleaseReply(released=True).model_dump_json().encode())


@pytest.fixture
async def bus(media_stack: MediaStack) -> AsyncIterator[Client]:
    client = await nats.connect(media_stack.nats_url)
    try:
        yield client
    finally:
        await client.close()


@pytest.fixture
async def gateway(bus: Client) -> AsyncIterator[FakeGateway]:
    fake = FakeGateway()
    subscriptions = [
        await bus.subscribe(ALL_CAMERA_SESSIONS, cb=fake.on_session),
        await bus.subscribe(ALL_CAMERA_RELEASES, cb=fake.on_release),
    ]
    await bus.flush()
    try:
        yield fake
    finally:
        for subscription in subscriptions:
            await subscription.unsubscribe()


async def api_get(stack: MediaStack, path: str) -> dict[str, Any]:
    """GET on MediaMTX's control API with the relay's credential (the test stack's secret)."""
    async with httpx.AsyncClient(timeout=5) as client:
        response = await client.get(f"{stack.api_url}{path}", auth=("relay", SECRET))
        response.raise_for_status()
        body: dict[str, Any] = response.json()
        return body


async def eventually(check: Any, within: float, interval: float = 0.5) -> Any:
    """Await ``check()`` until it returns something truthy (at most ``within`` seconds)."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + within
    while True:
        result = await check()
        if result:
            return result
        if loop.time() > deadline:
            return result
        await asyncio.sleep(interval)


def probe(stack: MediaStack, path: str) -> dict[str, Any]:
    """ffprobe of a relay path, run in the relay container."""
    result = stack.compose(
        "exec",
        "-T",
        "camera-relay",
        "ffprobe",
        "-v",
        "error",
        "-rtsp_transport",
        "tcp",
        "-show_entries",
        "stream=codec_name,profile,width,height,has_b_frames,r_frame_rate",
        "-of",
        "json",
        f"rtsp://relay:{SECRET}@mediamtx:8554/{path}",
        check=False,
    )
    streams = json.loads(result.stdout or "{}").get("streams") or [{}]
    stream: dict[str, Any] = streams[0]
    return stream
