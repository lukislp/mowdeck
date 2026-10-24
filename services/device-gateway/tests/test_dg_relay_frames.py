"""Photos through the camera relay (ADR 0045) and the order of ``capture_photo``: the
adapter's own photo, then the relay (the capture worker of ADR 0035 is removed).

``RelaySnapshotter`` is checked against an ``httpx.MockTransport`` playing the relay's frame tap.
"""

# ruff: noqa: S105, S106 - placeholder tokens, not credentials

import asyncio
import base64
import json
from typing import Any
from uuid import uuid4

import httpx
import pytest
from dg_support import SilentFleet, TestProvider, make_settings, registration, telemetry, wait_until
from mowdeck_contracts.v1 import (
    MowerRegistration,
    PhotoErrorCode,
    PhotoReply,
    PhotoRequest,
)
from mowdeck_contracts.v1.relay import VisionDemand
from mowdeck_contracts.v1.subjects import photo_subject
from mowdeck_control import (
    ConnectionConfig,
    MowerConnection,
    Photo,
    SimulatedMower,
    SimulationConfig,
)
from mowdeck_control_mammotion.cloud import MammotionCloudConnection
from mowdeck_control_mammotion.fake import FakeMammotionPort
from mowdeck_control_mammotion.options import CloudOptions
from mowdeck_control_mammotion.port import Camera, StreamSubscriptionResponse
from mowdeck_service_kit.bus import InMemoryBus
from mowdeck_service_kit.testing import lifespan_client

from device_gateway.adapters.control.snapshot import RelaySnapshotter
from device_gateway.application.ports import SnapshotFailed, SnapshotTarget, SnapshotUnavailable
from device_gateway.main import build_app, relay_snapshotter_from

RELAY_JPEG = b"\xff\xd8\xff\xe0relay-frame\xff\xd9"


class FakeRelay:
    """Plays ``RelaySnapshotter``: records the targets, answers a JPEG or raises."""

    def __init__(self, *, fail: SnapshotFailed | None = None, delay: float = 0.0) -> None:
        self.fail = fail
        self.delay = delay
        self.targets: list[tuple[SnapshotTarget, float]] = []
        self.active = 0
        self.max_active = 0

    async def snapshot(self, target: SnapshotTarget, *, timeout_seconds: float) -> Photo:
        self.targets.append((target, timeout_seconds))
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        try:
            if self.delay:
                await asyncio.sleep(self.delay)
            if self.fail is not None:
                raise self.fail
            return Photo(RELAY_JPEG, "image/jpeg")
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
    """A Mammotion cloud connection (PyMammotion's fake port): a live camera, no own photo."""
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


def _sim_without_photos(reg: MowerRegistration, *, camera: bool = True) -> TestProvider:
    def factory(registration: MowerRegistration) -> MowerConnection:
        return SimulatedMower(
            mower_id=registration.mower_id,
            account_id=registration.account_id,
            config=SimulationConfig(time_scale=60, photos=False, camera=camera),
        )

    return TestProvider({reg.mower_id: factory})


def _app(bus: InMemoryBus, provider: TestProvider, reg: MowerRegistration, **kw: Any) -> Any:
    return build_app(
        make_settings(bootstrap_mowers=[reg.model_dump(mode="json")], capture_timeout_seconds=7),
        bus=bus,
        connection_provider=provider,
        fleet=SilentFleet(),
        **kw,
    )


async def _ask(bus: InMemoryBus, reg: MowerRegistration, account_id: Any = None) -> PhotoReply:
    request = PhotoRequest(mower_id=reg.mower_id, account_id=account_id or reg.account_id)
    reply = await bus.request(photo_subject(reg.mower_id), request, timeout=3)
    return reply.model(PhotoReply)


async def _photo(
    reg: MowerRegistration, provider: TestProvider, account_id: Any = None, **kw: Any
) -> PhotoReply:
    bus = InMemoryBus()
    async with lifespan_client(_app(bus, provider, reg, **kw)):
        await wait_until(lambda: telemetry(bus, reg.mower_id), within=5.0)
        return await _ask(bus, reg, account_id)


async def test_dg_relay_the_adapters_own_photo_comes_first() -> None:
    reg = registration()
    relay = FakeRelay()
    reply = await _photo(reg, TestProvider(), relay_snapshotter=relay)
    assert reply.error is None
    assert base64.b64decode(reply.image_base64 or "") != RELAY_JPEG
    assert relay.targets == []


async def test_dg_relay_frames_of_a_mower_without_own_photos_come_from_the_relay() -> None:
    reg = registration()
    relay = FakeRelay()
    reply = await _photo(reg, _sim_without_photos(reg), relay_snapshotter=relay)
    assert reply.error is None, reply.error
    assert base64.b64decode(reply.image_base64 or "") == RELAY_JPEG
    [(target, timeout)] = relay.targets
    # Account and mower come from the gateway's own connection, never from the request.
    assert (target.account_id, target.mower_id) == (reg.account_id, reg.mower_id)
    assert target.demand is VisionDemand.ROUTINE
    assert timeout == 7


async def test_dg_relay_a_foreign_account_never_reaches_the_relay() -> None:
    reg = registration()
    relay = FakeRelay()
    reply = await _photo(reg, _sim_without_photos(reg), uuid4(), relay_snapshotter=relay)
    assert reply.error is not None and reply.error.code is PhotoErrorCode.NOT_FOUND
    assert relay.targets == []


async def test_dg_relay_cloud_mower_photo_comes_from_the_relay_without_a_vendor_session() -> None:
    port = _port_with_stream()
    port_reg, provider = _cloud_mower(port)
    relay = FakeRelay()
    reply = await _photo(port_reg, provider, relay_snapshotter=relay)
    assert reply.error is None, reply.error
    assert base64.b64decode(reply.image_base64 or "") == RELAY_JPEG
    [(target, _)] = relay.targets
    assert (target.account_id, target.mower_id) == (port_reg.account_id, port_reg.mower_id)
    assert port.stream_requests == 0, "the relay asks for its own vendor session"


async def test_dg_relay_an_unavailable_relay_fails_a_cloud_mower_with_its_reason() -> None:
    port = _port_with_stream()
    port_reg, provider = _cloud_mower(port)
    relay = FakeRelay(fail=SnapshotUnavailable("the camera relay is unreachable: ConnectError"))
    reply = await _photo(port_reg, provider, relay_snapshotter=relay)
    assert reply.error is not None and reply.error.code is PhotoErrorCode.FAILED
    assert "unreachable" in reply.error.detail
    assert len(relay.targets) == 1
    assert port.stream_requests == 0, "no capture worker joins the vendor channel any more"


async def test_dg_relay_a_refusal_of_the_relay_is_final() -> None:
    port_reg, provider = _cloud_mower(_port_with_stream())
    relay = FakeRelay(fail=SnapshotFailed("the camera relay answered 409 camera-budget"))
    reply = await _photo(port_reg, provider, relay_snapshotter=relay)
    assert reply.error is not None and reply.error.code is PhotoErrorCode.FAILED
    assert "camera-budget" in reply.error.detail
    assert len(relay.targets) == 1


async def test_dg_relay_runs_one_photo_per_mower_at_a_time() -> None:
    port_reg, provider = _cloud_mower(_port_with_stream())
    relay = FakeRelay(delay=0.1)
    bus = InMemoryBus()
    async with lifespan_client(_app(bus, provider, port_reg, relay_snapshotter=relay)):
        await wait_until(lambda: telemetry(bus, port_reg.mower_id), within=5.0)
        replies = await asyncio.gather(_ask(bus, port_reg), _ask(bus, port_reg))
    assert all(r.error is None for r in replies)
    assert relay.max_active == 1


async def test_dg_relay_unavailable_fails_with_the_relays_reason() -> None:
    reg = registration()
    relay = FakeRelay(fail=SnapshotUnavailable("the camera relay answered 503 frames-disabled"))
    reply = await _photo(reg, _sim_without_photos(reg), relay_snapshotter=relay)
    assert reply.error is not None and reply.error.code is PhotoErrorCode.FAILED
    assert "frames-disabled" in reply.error.detail


async def test_dg_relay_a_mower_without_a_camera_is_unsupported() -> None:
    reg = registration()
    relay = FakeRelay()
    reply = await _photo(reg, _sim_without_photos(reg, camera=False), relay_snapshotter=relay)
    assert reply.error is not None and reply.error.code is PhotoErrorCode.UNSUPPORTED
    assert relay.targets == []


async def test_dg_relay_without_any_frame_source_a_mower_without_photos_is_unsupported() -> None:
    reg = registration()
    reply = await _photo(reg, _sim_without_photos(reg))
    assert reply.error is not None and reply.error.code is PhotoErrorCode.UNSUPPORTED


async def test_dg_relay_without_the_relay_a_cloud_mower_is_unsupported() -> None:
    port = _port_with_stream()
    port_reg, provider = _cloud_mower(port)
    reply = await _photo(port_reg, provider)
    assert reply.error is not None and reply.error.code is PhotoErrorCode.UNSUPPORTED
    assert port.stream_requests == 0


def test_dg_relay_settings_of_the_removed_capture_worker_are_ignored() -> None:
    settings = make_settings(capture_url="http://vision-capture:8000", capture_token="left-over")
    assert not hasattr(settings, "capture_url")
    assert not hasattr(settings, "capture_token")
    assert relay_snapshotter_from(settings) is None


def _port_with_stream() -> FakeMammotionPort:
    port = FakeMammotionPort()
    port.stream = _subscription()
    return port


# --- RelaySnapshotter over HTTP ----------------------------------------------------------------


def _target() -> SnapshotTarget:
    return SnapshotTarget(account_id=uuid4(), mower_id=uuid4())


async def test_dg_relay_snapshotter_posts_the_gateways_mower_with_its_token() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, content=RELAY_JPEG, headers={"Content-Type": "image/jpeg"})

    snapshotter = RelaySnapshotter(
        "http://camera-relay:8000/",
        token="frames-t0k",
        max_width=800,
        transport=httpx.MockTransport(handler),
    )
    target = _target()
    photo = await snapshotter.snapshot(target, timeout_seconds=45)
    assert photo == Photo(RELAY_JPEG, "image/jpeg")
    [request] = seen
    assert str(request.url) == "http://camera-relay:8000/internal/v1/frames/snapshot"
    assert request.headers["Authorization"] == "Bearer frames-t0k"
    body = json.loads(request.content)
    assert body["account_id"] == str(target.account_id)
    assert body["mower_id"] == str(target.mower_id)
    assert body["max_width"] == 800
    assert body["timeout_s"] == 30, "the relay holds a snapshot lease at most 30 s"
    assert body["demand"] == "routine"
    assert body["requested_by"]["kind"] == "service"
    assert body["requested_by"]["display_name"] == "device-gateway"


def _problem(status: int, kind: str, detail: str = "") -> httpx.Response:
    return httpx.Response(
        status,
        json={
            "type": f"urn:mowdeck:problem:{kind}",
            "title": "x",
            "status": status,
            "detail": detail,
        },
        headers={"Content-Type": "application/problem+json"},
    )


@pytest.mark.parametrize(
    ("response", "error", "text"),
    [
        (_problem(503, "frames-disabled"), SnapshotUnavailable, "frames-disabled"),
        (_problem(503, "camera-relay-unsupported"), SnapshotUnavailable, "unsupported"),
        (httpx.Response(401), SnapshotUnavailable, "401"),
        (httpx.Response(404), SnapshotUnavailable, "404"),
        (_problem(409, "camera-budget", "battery below 15 %"), SnapshotFailed, "battery"),
        (_problem(503, "camera-unavailable", "offline"), SnapshotFailed, "camera-unavailable"),
        (_problem(404, "not-found", "mower not found"), SnapshotFailed, "not-found"),
        (_problem(504, "frame-timeout"), SnapshotFailed, "frame-timeout"),
        (
            httpx.Response(200, content=b"", headers={"Content-Type": "image/jpeg"}),
            SnapshotFailed,
            "empty",
        ),
    ],
)
async def test_dg_relay_snapshotter_maps_the_relays_answers(
    response: httpx.Response, error: type[SnapshotFailed], text: str
) -> None:
    snapshotter = RelaySnapshotter(
        "http://r", token="t", transport=httpx.MockTransport(lambda request: response)
    )
    with pytest.raises(SnapshotFailed, match=text) as raised:
        await snapshotter.snapshot(_target(), timeout_seconds=1)
    assert type(raised.value) is error


async def test_dg_relay_snapshotter_unreachable_hands_over_and_slow_is_final() -> None:
    def unreachable(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    def slow(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow", request=request)

    with pytest.raises(SnapshotUnavailable, match="unreachable"):
        await RelaySnapshotter(
            "http://r", token="t", transport=httpx.MockTransport(unreachable)
        ).snapshot(_target(), timeout_seconds=1)
    with pytest.raises(SnapshotFailed, match="in time") as raised:
        await RelaySnapshotter("http://r", token="t", transport=httpx.MockTransport(slow)).snapshot(
            _target(), timeout_seconds=1
        )
    assert type(raised.value) is SnapshotFailed


def test_dg_relay_snapshotter_from_settings() -> None:
    assert relay_snapshotter_from(make_settings()) is None
    assert relay_snapshotter_from(make_settings(relay_url="http://camera-relay:8000")) is None
    built = relay_snapshotter_from(
        make_settings(relay_url="http://camera-relay:8000", relay_frames_token="tok")
    )
    assert isinstance(built, RelaySnapshotter)
    assert built.url == "http://camera-relay:8000/internal/v1/frames/snapshot"
    assert built.token == "tok"


async def test_dg_relay_one_photo_per_mower_at_a_time_also_through_the_relay() -> None:
    reg = registration()

    class SlowRelay(FakeRelay):
        active = 0
        peak = 0

        async def snapshot(self, target: SnapshotTarget, *, timeout_seconds: float) -> Photo:
            SlowRelay.active += 1
            SlowRelay.peak = max(SlowRelay.peak, SlowRelay.active)
            try:
                await asyncio.sleep(0.05)
                return await super().snapshot(target, timeout_seconds=timeout_seconds)
            finally:
                SlowRelay.active -= 1

    relay = SlowRelay()
    bus = InMemoryBus()
    async with lifespan_client(_app(bus, _sim_without_photos(reg), reg, relay_snapshotter=relay)):
        await wait_until(lambda: telemetry(bus, reg.mower_id), within=5.0)
        replies = await asyncio.gather(_ask(bus, reg), _ask(bus, reg))
    assert all(r.error is None for r in replies)
    assert SlowRelay.peak == 1


async def test_dg_relay_the_photo_requests_demand_reaches_the_relay() -> None:
    reg = registration()
    relay = FakeRelay()
    bus = InMemoryBus()
    async with lifespan_client(_app(bus, _sim_without_photos(reg), reg, relay_snapshotter=relay)):
        await wait_until(lambda: telemetry(bus, reg.mower_id), within=5.0)
        request = PhotoRequest(
            mower_id=reg.mower_id, account_id=reg.account_id, demand=VisionDemand.ERROR_ANALYSIS
        )
        reply = (await bus.request(photo_subject(reg.mower_id), request, timeout=3)).model(
            PhotoReply
        )
    assert reply.error is None
    [(target, _)] = relay.targets
    assert target.demand is VisionDemand.ERROR_ANALYSIS
