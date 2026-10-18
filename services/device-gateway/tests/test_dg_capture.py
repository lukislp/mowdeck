"""Photos from a live camera through the capture worker (ADR 0035).

The Mammotion cloud connection (on PyMammotion's fake port) offers an Agora camera session but no
``capture_photo``; with a ``CameraSnapshotter`` the gateway answers photo requests with a frame
of the front camera, one capture per mower at a time. ``HttpCameraSnapshotter`` is checked
against an ``httpx.MockTransport`` playing the worker.
"""

# ruff: noqa: S105, S106 - placeholder tokens, not credentials

import asyncio
import base64
import json
from typing import Any
from uuid import uuid4

import httpx
import pytest
from dg_support import (
    SilentFleet,
    TestProvider,
    make_settings,
    registration,
    telemetry,
    wait_until,
)
from mowdeck_contracts.v1 import (
    CameraInfo,
    CameraProvider,
    CameraSession,
    MowerRegistration,
    PhotoErrorCode,
    PhotoReply,
    PhotoRequest,
    utc_now,
)
from mowdeck_contracts.v1.subjects import photo_subject
from mowdeck_control import ConnectionConfig, MowerConnection, Photo
from mowdeck_control_mammotion.cloud import MammotionCloudConnection
from mowdeck_control_mammotion.fake import FakeMammotionPort
from mowdeck_control_mammotion.options import CloudOptions
from mowdeck_control_mammotion.port import Camera, StreamSubscriptionResponse
from mowdeck_service_kit.bus import InMemoryBus
from mowdeck_service_kit.testing import lifespan_client

from device_gateway.adapters.control.snapshot import HttpCameraSnapshotter
from device_gateway.application.ports import SnapshotFailed, SnapshotTarget, SnapshotUnavailable
from device_gateway.application.session import snapshot_camera_uid
from device_gateway.main import build_app, snapshotter_from

JPEG = b"\xff\xd8\xff\xe0fake-jpeg\xff\xd9"


class FakeSnapshotter:
    """Records requests and answers with a JPEG (or fails); plays the capture worker (it needs
    the vendor's camera session)."""

    needs_camera_session = True

    def __init__(self, *, fail: str | None = None, delay: float = 0.0) -> None:
        self.fail = fail
        self.delay = delay
        self.calls: list[tuple[CameraSession, int | None, float]] = []
        self.active = 0
        self.max_active = 0

    async def snapshot(self, target: SnapshotTarget, *, timeout_seconds: float) -> Photo:
        assert target.session is not None
        self.calls.append((target.session, target.camera_uid, timeout_seconds))
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        try:
            if self.delay:
                await asyncio.sleep(self.delay)
            if self.fail:
                raise SnapshotFailed(self.fail)
            return Photo(JPEG, "image/jpeg")
        finally:
            self.active -= 1


def _subscription() -> StreamSubscriptionResponse:
    return StreamSubscriptionResponse(
        appid="app-id",
        openEncrypt=0,
        cameras=[Camera(cameraId=0, token="cam-0"), Camera(cameraId=1, token="cam-1")],
        channelName="channel-x",
        areaCode="GLOB",
        token="007-not-a-real-token",
        uid=4242,
    )


def _cloud_mower(port: FakeMammotionPort) -> tuple[MowerRegistration, TestProvider]:
    reg = registration()

    def factory(registration: MowerRegistration) -> MowerConnection:
        options = CloudOptions.parse(
            {"account": "second@example.com", "password": "pw", "device_name": "Luba-VPDRYRUN"}
        )
        config = ConnectionConfig(
            mower_id=registration.mower_id, account_id=registration.account_id, options={}
        )
        return MammotionCloudConnection(port, config, options)

    return reg, TestProvider({reg.mower_id: factory}, tick_seconds=None)


async def _ask(bus: InMemoryBus, reg: MowerRegistration) -> PhotoReply:
    request = PhotoRequest(mower_id=reg.mower_id, account_id=reg.account_id)
    reply = await bus.request(photo_subject(reg.mower_id), request, timeout=3)
    return reply.model(PhotoReply)


def _app(bus: InMemoryBus, provider: TestProvider, reg: MowerRegistration, **kw: Any) -> Any:
    return build_app(
        make_settings(bootstrap_mowers=[reg.model_dump(mode="json")], capture_timeout_seconds=7),
        bus=bus,
        connection_provider=provider,
        fleet=SilentFleet(),
        **kw,
    )


async def _connected(bus: InMemoryBus, reg: MowerRegistration) -> None:
    await wait_until(lambda: telemetry(bus, reg.mower_id), within=5.0)


async def test_dg_capture_cloud_mower_photo_is_a_camera_frame() -> None:
    port = FakeMammotionPort()
    port.stream = _subscription()
    reg, provider = _cloud_mower(port)
    snapshotter = FakeSnapshotter()
    bus = InMemoryBus()
    async with lifespan_client(_app(bus, provider, reg, snapshotter=snapshotter)):
        await _connected(bus, reg)
        reply = await _ask(bus, reg)
    assert reply.error is None, reply.error
    assert reply.media_type == "image/jpeg"
    assert reply.image_base64 is not None and base64.b64decode(reply.image_base64) == JPEG
    [(session, uid, timeout)] = snapshotter.calls
    assert (session.app_id, session.channel, session.uid) == ("app-id", "channel-x", 4242)
    assert uid == 1, "the front (left) camera, slot 0"
    assert timeout == 7
    assert port.stream_requests == 1


async def test_dg_capture_without_worker_is_unsupported_and_failures_are_reported() -> None:
    port = FakeMammotionPort()
    port.stream = _subscription()
    reg, provider = _cloud_mower(port)
    bus = InMemoryBus()
    async with lifespan_client(_app(bus, provider, reg)):
        await _connected(bus, reg)
        unsupported = await _ask(bus, reg)
    assert unsupported.error is not None and unsupported.error.code is PhotoErrorCode.UNSUPPORTED

    bus = InMemoryBus()
    reg, provider = _cloud_mower(port)
    failing = FakeSnapshotter(fail="the capture worker answered 502")
    async with lifespan_client(_app(bus, provider, reg, snapshotter=failing)):
        await _connected(bus, reg)
        failed = await _ask(bus, reg)
        port.stream = None  # the cloud gives no token: the camera is not reachable
        offline = await _ask(bus, reg)
    assert failed.error is not None and failed.error.code is PhotoErrorCode.FAILED
    assert "502" in failed.error.detail
    assert offline.error is not None and offline.error.code is PhotoErrorCode.NOT_CONNECTED


async def test_dg_capture_runs_one_capture_per_mower_at_a_time() -> None:
    port = FakeMammotionPort()
    port.stream = _subscription()
    reg, provider = _cloud_mower(port)
    snapshotter = FakeSnapshotter(delay=0.1)
    bus = InMemoryBus()
    async with lifespan_client(_app(bus, provider, reg, snapshotter=snapshotter)):
        await _connected(bus, reg)
        replies = await asyncio.gather(_ask(bus, reg), _ask(bus, reg))
    assert all(r.error is None for r in replies)
    assert snapshotter.max_active == 1


async def test_dg_capture_simulator_keeps_its_own_photo() -> None:
    reg = registration()
    snapshotter = FakeSnapshotter()
    bus = InMemoryBus()
    provider = TestProvider()
    async with lifespan_client(_app(bus, provider, reg, snapshotter=snapshotter)):
        await _connected(bus, reg)
        reply = await _ask(bus, reg)
    assert reply.error is None
    assert snapshotter.calls == [], "PhotoCapture of the adapter wins"


def _session(**changes: Any) -> CameraSession:
    values: dict[str, Any] = {
        "provider": CameraProvider.AGORA,
        "app_id": "app",
        "channel": "chan",
        "token": "secret-token",
        "uid": 7,
        "cameras": [CameraInfo(id="2", name="Rear", uid=None), CameraInfo(id="0", name="F", uid=1)],
        "expires_at": utc_now(),
    }
    values.update(changes)
    return CameraSession.model_validate(values)


def _target(session: CameraSession | None, uid: int | None) -> SnapshotTarget:
    return SnapshotTarget(account_id=uuid4(), mower_id=uuid4(), session=session, camera_uid=uid)


async def test_dg_capture_http_snapshotter_needs_a_session() -> None:
    snapshotter = HttpCameraSnapshotter("http://c")
    with pytest.raises(SnapshotUnavailable):
        await snapshotter.snapshot(_target(None, None), timeout_seconds=1)


def test_dg_capture_camera_choice() -> None:
    assert snapshot_camera_uid(_session()) == 1
    assert snapshot_camera_uid(_session(cameras=[])) is None


async def test_dg_capture_http_snapshotter_posts_the_session_to_the_worker() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, content=JPEG, headers={"Content-Type": "image/jpeg"})

    snapshotter = HttpCameraSnapshotter(
        "http://capture:8000/", token="t0k", transport=httpx.MockTransport(handler)
    )
    photo = await snapshotter.snapshot(_target(_session(), 1), timeout_seconds=12)
    assert photo == Photo(JPEG, "image/jpeg")
    [request] = seen
    assert str(request.url) == "http://capture:8000/api/v1/capture"
    assert request.headers["Authorization"] == "Bearer t0k"
    body = json.loads(request.content)
    assert body["camera_uid"] == 1 and body["timeout_seconds"] == 12
    assert body["session"]["token"] == "secret-token"


async def test_dg_capture_http_snapshotter_maps_worker_problems() -> None:
    def refuse(request: httpx.Request) -> httpx.Response:
        return httpx.Response(502, json={"type": "urn:mowdeck:problem:capture-failed"})

    def unreachable(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    def slow(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow", request=request)

    def empty(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"", headers={"Content-Type": "image/jpeg"})

    for handler, text in [
        (refuse, "answered 502"),
        (unreachable, "unreachable"),
        (slow, "in time"),
        (empty, "empty"),
    ]:
        snapshotter = HttpCameraSnapshotter("http://c", transport=httpx.MockTransport(handler))
        with pytest.raises(SnapshotFailed, match=text):
            await snapshotter.snapshot(_target(_session(), None), timeout_seconds=1)


def test_dg_capture_snapshotter_from_settings() -> None:
    assert snapshotter_from(make_settings()) is None
    built = snapshotter_from(
        make_settings(capture_url="http://vision-capture:8000", capture_token="x")
    )
    assert isinstance(built, HttpCameraSnapshotter)
    assert built.url == "http://vision-capture:8000/api/v1/capture" and built.token == "x"
