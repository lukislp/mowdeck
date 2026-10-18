"""Photos through the camera relay (ADR 0045) and the fallback order of ``capture_photo``:
the adapter's own photo, then the relay, then the deprecated capture worker.

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
from mowdeck_contracts.v1.subjects import photo_subject
from mowdeck_control import MowerConnection, Photo, SimulatedMower, SimulationConfig
from mowdeck_control_mammotion.fake import FakeMammotionPort
from mowdeck_service_kit.bus import InMemoryBus
from mowdeck_service_kit.testing import lifespan_client
from test_dg_capture import JPEG, FakeSnapshotter, _cloud_mower, _subscription

from device_gateway.adapters.control.snapshot import RelaySnapshotter
from device_gateway.application.ports import SnapshotFailed, SnapshotTarget, SnapshotUnavailable
from device_gateway.main import build_app, relay_snapshotter_from

RELAY_JPEG = b"\xff\xd8\xff\xe0relay-frame\xff\xd9"


class FakeRelay:
    """Plays ``RelaySnapshotter``: records the targets, answers a JPEG or raises."""

    needs_camera_session = False

    def __init__(self, *, fail: SnapshotFailed | None = None) -> None:
        self.fail = fail
        self.targets: list[tuple[SnapshotTarget, float]] = []

    async def snapshot(self, target: SnapshotTarget, *, timeout_seconds: float) -> Photo:
        self.targets.append((target, timeout_seconds))
        if self.fail is not None:
            raise self.fail
        return Photo(RELAY_JPEG, "image/jpeg")


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
    worker = FakeSnapshotter()
    reply = await _photo(reg, _sim_without_photos(reg), relay_snapshotter=relay, snapshotter=worker)
    assert reply.error is None, reply.error
    assert base64.b64decode(reply.image_base64 or "") == RELAY_JPEG
    [(target, timeout)] = relay.targets
    # Account and mower come from the gateway's own connection, never from the request.
    assert (target.account_id, target.mower_id) == (reg.account_id, reg.mower_id)
    assert target.session is None, "the relay asks for its own vendor session"
    assert timeout == 7
    assert worker.calls == []


async def test_dg_relay_a_foreign_account_never_reaches_the_relay() -> None:
    reg = registration()
    relay = FakeRelay()
    reply = await _photo(reg, _sim_without_photos(reg), uuid4(), relay_snapshotter=relay)
    assert reply.error is not None and reply.error.code is PhotoErrorCode.NOT_FOUND
    assert relay.targets == []


async def test_dg_relay_an_unavailable_relay_hands_over_to_the_capture_worker() -> None:
    port_reg, provider = _cloud_mower(_port_with_stream())
    relay = FakeRelay(fail=SnapshotUnavailable("the camera relay is unreachable: ConnectError"))
    worker = FakeSnapshotter()
    reply = await _photo(port_reg, provider, relay_snapshotter=relay, snapshotter=worker)
    assert reply.error is None, reply.error
    assert base64.b64decode(reply.image_base64 or "") == JPEG
    assert len(relay.targets) == 1
    assert len(worker.calls) == 1


async def test_dg_relay_a_refusal_of_the_relay_is_final() -> None:
    port_reg, provider = _cloud_mower(_port_with_stream())
    relay = FakeRelay(fail=SnapshotFailed("the camera relay answered 409 camera-budget"))
    worker = FakeSnapshotter()
    reply = await _photo(port_reg, provider, relay_snapshotter=relay, snapshotter=worker)
    assert reply.error is not None and reply.error.code is PhotoErrorCode.FAILED
    assert "camera-budget" in reply.error.detail
    assert worker.calls == [], "a limit of the relay is never bypassed through the worker"


async def test_dg_relay_unavailable_without_a_worker_fails_with_the_relays_reason() -> None:
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


async def test_dg_relay_the_worker_alone_still_works_for_cloud_mowers() -> None:
    port_reg, provider = _cloud_mower(_port_with_stream())
    worker = FakeSnapshotter()
    reply = await _photo(port_reg, provider, snapshotter=worker)
    assert reply.error is None
    assert len(worker.calls) == 1


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
