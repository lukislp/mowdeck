"""Frame sets for Vision (ADR 0045): a photo request with ``max_frames`` 2 gets one frame of each
front camera of the same moment through the camera relay, left first, with side and capture
time; every other source keeps its single frame; the HTTP side of ``RelaySnapshotter.frame_set``
against an ``httpx.MockTransport`` playing the relay."""

# ruff: noqa: S106 - placeholder tokens, not credentials

import base64
import json
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

import httpx
import pytest
from dg_support import TestProvider, registration, telemetry, wait_until
from mowdeck_contracts.v1 import (
    MowerRegistration,
    PhotoErrorCode,
    PhotoReply,
    PhotoRequest,
    VisionFrame,
    VisionFrameMissing,
    VisionFrameMissingReason,
    VisionFrameSetReply,
)
from mowdeck_contracts.v1.relay import VisionDemand
from mowdeck_contracts.v1.subjects import photo_subject
from mowdeck_control import Photo
from mowdeck_service_kit.bus import InMemoryBus
from mowdeck_service_kit.testing import lifespan_client
from test_dg_relay_frames import (
    RELAY_JPEG,
    FakeRelay,
    _app,
    _cloud_mower,
    _port_with_stream,
    _sim_without_photos,
)

from device_gateway.adapters.control.snapshot import RelaySnapshotter
from device_gateway.application.ports import (
    CameraFrame,
    CameraFrames,
    SnapshotFailed,
    SnapshotTarget,
    SnapshotUnavailable,
)

AT = datetime(2026, 10, 18, 12, 0, tzinfo=UTC)
LEFT_JPEG = b"\xff\xd8\xff\xe0left-camera\xff\xd9"
RIGHT_JPEG = b"\xff\xd8\xff\xe0right-camera\xff\xd9"
PAIR = CameraFrames(
    frames=(
        CameraFrame(Photo(LEFT_JPEG, "image/jpeg"), AT, "front_left", "Front left", "left"),
        CameraFrame(
            Photo(RIGHT_JPEG, "image/jpeg"),
            AT + timedelta(milliseconds=60),
            "front_right",
            "Front right",
            "right",
        ),
    )
)


class FakeSetRelay(FakeRelay):
    """A relay that also takes frame sets."""

    def __init__(self, found: CameraFrames = PAIR, **kw: Any) -> None:
        super().__init__(**kw)
        self.found = found
        self.sets: list[tuple[SnapshotTarget, int, int, float]] = []

    async def frame_set(
        self,
        target: SnapshotTarget,
        *,
        max_frames: int,
        max_bytes: int,
        timeout_seconds: float,
    ) -> CameraFrames:
        self.sets.append((target, max_frames, max_bytes, timeout_seconds))
        if self.fail is not None:
            raise self.fail
        return self.found


async def _ask(
    reg: MowerRegistration, provider: TestProvider, *, max_frames: int = 2, **kw: Any
) -> PhotoReply:
    bus = InMemoryBus()
    async with lifespan_client(_app(bus, provider, reg, **kw)):
        await wait_until(lambda: telemetry(bus, reg.mower_id), within=5.0)
        request = PhotoRequest(
            mower_id=reg.mower_id,
            account_id=reg.account_id,
            demand=VisionDemand.ERROR_ANALYSIS,
            max_frames=max_frames,
        )
        reply = await bus.request(photo_subject(reg.mower_id), request, timeout=3)
        return reply.model(PhotoReply)


async def test_dg_set_both_front_cameras_reach_vision_left_first_with_their_times() -> None:
    reg = registration()
    relay = FakeSetRelay()
    reply = await _ask(reg, _sim_without_photos(reg), relay_snapshotter=relay)
    assert reply.error is None, reply.error
    assert reply.image_base64 is None, "a multi-frame reply carries frames only"
    assert [(f.side, f.camera_id, f.camera_name) for f in reply.frames] == [
        ("left", "front_left", "Front left"),
        ("right", "front_right", "Front right"),
    ]
    assert [base64.b64decode(f.image_base64) for f in reply.frames] == [LEFT_JPEG, RIGHT_JPEG]
    assert [f.captured_at for f in reply.frames] == [AT, AT + timedelta(milliseconds=60)]
    assert reply.frames_missing == []
    [(target, max_frames, max_bytes, timeout)] = relay.sets
    assert (target.account_id, target.mower_id) == (reg.account_id, reg.mower_id)
    assert target.demand is VisionDemand.ERROR_ANALYSIS
    assert max_frames == 2
    assert max_bytes == PhotoRequest.model_fields["max_bytes"].default // 2, (
        "each frame gets half of the reply's cap"
    )
    assert timeout == 7
    assert relay.targets == [], "no single snapshot besides the set"


async def test_dg_set_a_silent_camera_is_named_and_the_other_frame_served() -> None:
    reg = registration()
    relay = FakeSetRelay(CameraFrames(frames=PAIR.frames[:1], missing=("front_right",)))
    reply = await _ask(reg, _sim_without_photos(reg), relay_snapshotter=relay)
    assert reply.error is None
    assert [f.side for f in reply.frames] == ["left"]
    assert reply.frames_missing == ["front_right"]


async def test_dg_set_frames_beyond_the_reply_cap_are_left_out() -> None:
    reg = registration()
    relay = FakeSetRelay()
    bus = InMemoryBus()
    async with lifespan_client(_app(bus, _sim_without_photos(reg), reg, relay_snapshotter=relay)):
        await wait_until(lambda: telemetry(bus, reg.mower_id), within=5.0)
        request = PhotoRequest(
            mower_id=reg.mower_id, account_id=reg.account_id, max_frames=2, max_bytes=1024
        )
        big = CameraFrame(Photo(b"\xff" * 2000, "image/jpeg"), AT, "front_left", None, "left")
        relay.found = CameraFrames(frames=(PAIR.frames[0], PAIR.frames[1]))
        reply = (await bus.request(photo_subject(reg.mower_id), request, timeout=3)).model(
            PhotoReply
        )
        assert [f.side for f in reply.frames] == ["left", "right"], "both fit"
        relay.found = CameraFrames(frames=(PAIR.frames[0], replace_photo(PAIR.frames[1], 1200)))
        reply = (await bus.request(photo_subject(reg.mower_id), request, timeout=3)).model(
            PhotoReply
        )
        assert [f.side for f in reply.frames] == ["left"]
        assert reply.frames_missing == ["front_right"]
        relay.found = CameraFrames(frames=(big,))
        reply = (await bus.request(photo_subject(reg.mower_id), request, timeout=3)).model(
            PhotoReply
        )
        assert reply.error is not None and reply.error.code is PhotoErrorCode.TOO_LARGE


def replace_photo(frame: CameraFrame, size: int) -> CameraFrame:
    return CameraFrame(
        Photo(b"\xff" * size, "image/jpeg"),
        frame.captured_at,
        frame.camera_id,
        frame.camera_name,
        frame.side,
    )


async def test_dg_set_a_single_frame_request_keeps_the_single_snapshot() -> None:
    reg = registration()
    relay = FakeSetRelay()
    reply = await _ask(reg, _sim_without_photos(reg), max_frames=1, relay_snapshotter=relay)
    assert base64.b64decode(reply.image_base64 or "") == RELAY_JPEG
    assert reply.frames == []
    assert relay.sets == []
    assert len(relay.targets) == 1


async def test_dg_set_the_adapters_own_photo_is_a_set_of_one() -> None:
    reg = registration()
    relay = FakeSetRelay()
    reply = await _ask(reg, TestProvider(), relay_snapshotter=relay)
    assert reply.error is None
    [frame] = reply.frames
    assert frame.side is None and frame.captured_at is None
    assert relay.sets == [] and relay.targets == []


async def test_dg_set_a_source_without_sets_gives_its_single_frame() -> None:
    port_reg, provider = _cloud_mower(_port_with_stream())
    relay = FakeRelay()
    reply = await _ask(port_reg, provider, relay_snapshotter=relay)
    assert reply.error is None
    assert len(reply.frames) == 1
    assert len(relay.targets) == 1


async def test_dg_set_an_unavailable_relay_and_a_refusal_both_fail_the_set() -> None:
    port_reg, provider = _cloud_mower(_port_with_stream())
    relay = FakeSetRelay(fail=SnapshotUnavailable("the camera relay answered 503 frames-disabled"))
    reply = await _ask(port_reg, provider, relay_snapshotter=relay)
    assert reply.error is not None and reply.error.code is PhotoErrorCode.FAILED
    assert "frames-disabled" in reply.error.detail
    assert len(relay.sets) == 1

    port_reg, provider = _cloud_mower(_port_with_stream())
    relay = FakeSetRelay(fail=SnapshotFailed("the camera relay answered 409 camera-budget"))
    reply = await _ask(port_reg, provider, relay_snapshotter=relay)
    assert reply.error is not None and reply.error.code is PhotoErrorCode.FAILED
    assert "camera-budget" in reply.error.detail


async def test_dg_set_a_foreign_account_never_reaches_the_relay() -> None:
    reg = registration()
    relay = FakeSetRelay()
    bus = InMemoryBus()
    async with lifespan_client(_app(bus, _sim_without_photos(reg), reg, relay_snapshotter=relay)):
        await wait_until(lambda: telemetry(bus, reg.mower_id), within=5.0)
        request = PhotoRequest(mower_id=reg.mower_id, account_id=uuid4(), max_frames=2)
        reply = (await bus.request(photo_subject(reg.mower_id), request, timeout=3)).model(
            PhotoReply
        )
    assert reply.error is not None and reply.error.code is PhotoErrorCode.NOT_FOUND
    assert relay.sets == []


# --- RelaySnapshotter.frame_set over HTTP ------------------------------------------------------


def _target() -> SnapshotTarget:
    return SnapshotTarget(account_id=uuid4(), mower_id=uuid4(), demand=VisionDemand.ERROR_ANALYSIS)


def _set_reply() -> VisionFrameSetReply:
    return VisionFrameSetReply(
        frames=[
            VisionFrame(
                camera_id="front_left",
                camera_name="Front left",
                side="left",
                image_base64=base64.b64encode(LEFT_JPEG).decode(),
                captured_at=AT,
                received_at=AT,
            ),
        ],
        missing=[
            VisionFrameMissing(
                camera_id="front_right", side="right", reason=VisionFrameMissingReason.SILENT
            )
        ],
    )


async def test_dg_set_snapshotter_posts_the_set_request_and_reads_the_frames() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            200,
            content=_set_reply().model_dump_json(),
            headers={"Content-Type": "application/json"},
        )

    snapshotter = RelaySnapshotter(
        "http://camera-relay:8000/",
        token="frames-t0k",
        max_width=800,
        transport=httpx.MockTransport(handler),
    )
    target = _target()
    found = await snapshotter.frame_set(target, max_frames=2, max_bytes=200_000, timeout_seconds=45)
    assert found.frames == (
        CameraFrame(Photo(LEFT_JPEG, "image/jpeg"), AT, "front_left", "Front left", "left"),
    )
    assert found.missing == ("front_right",)
    [request] = seen
    assert str(request.url) == "http://camera-relay:8000/internal/v1/frames/set"
    assert request.headers["Authorization"] == "Bearer frames-t0k"
    body = json.loads(request.content)
    assert body["account_id"] == str(target.account_id)
    assert body["mower_id"] == str(target.mower_id)
    assert body["max_frames"] == 2
    assert body["max_bytes"] == 200_000
    assert body["max_width"] == 800
    assert body["timeout_s"] == 30
    assert body["demand"] == "error_analysis"
    assert body["requested_by"]["kind"] == "service"


async def test_dg_set_snapshotter_falls_back_to_one_frame_on_an_older_relay() -> None:
    paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        if request.url.path.endswith("/set"):
            return httpx.Response(404, json={"detail": "Not Found"})
        return httpx.Response(200, content=RELAY_JPEG, headers={"Content-Type": "image/jpeg"})

    snapshotter = RelaySnapshotter("http://r", token="t", transport=httpx.MockTransport(handler))
    found = await snapshotter.frame_set(_target(), max_frames=2, max_bytes=1024, timeout_seconds=5)
    assert found == CameraFrames(frames=(CameraFrame(Photo(RELAY_JPEG, "image/jpeg")),))
    assert paths == ["/internal/v1/frames/set", "/internal/v1/frames/snapshot"]


def _bad_base64() -> str:
    reply = _set_reply()
    frame = reply.frames[0].model_copy(update={"image_base64": "!"})
    return reply.model_copy(update={"frames": [frame]}).model_dump_json()


def _problem(status: int, kind: str) -> httpx.Response:
    return httpx.Response(
        status,
        json={"type": f"urn:mowdeck:problem:{kind}", "title": "x", "status": status},
        headers={"Content-Type": "application/problem+json"},
    )


@pytest.mark.parametrize(
    ("response", "error", "text"),
    [
        (_problem(503, "frames-disabled"), SnapshotUnavailable, "frames-disabled"),
        (httpx.Response(401), SnapshotUnavailable, "401"),
        (_problem(409, "camera-budget"), SnapshotFailed, "camera-budget"),
        (_problem(404, "not-found"), SnapshotFailed, "not-found"),
        (_problem(504, "frame-timeout"), SnapshotFailed, "frame-timeout"),
        (httpx.Response(200, content=b"{}"), SnapshotFailed, "unreadable"),
        (httpx.Response(200, content=_bad_base64()), SnapshotFailed, "unreadable"),
    ],
)
async def test_dg_set_snapshotter_maps_the_relays_answers(
    response: httpx.Response, error: type[SnapshotFailed], text: str
) -> None:
    snapshotter = RelaySnapshotter(
        "http://r", token="t", transport=httpx.MockTransport(lambda request: response)
    )
    with pytest.raises(SnapshotFailed, match=text) as raised:
        await snapshotter.frame_set(_target(), max_frames=2, max_bytes=1024, timeout_seconds=1)
    assert type(raised.value) is error
