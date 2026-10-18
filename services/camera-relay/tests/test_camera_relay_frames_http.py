"""The frame tap's HTTP routes, the mower states from telemetry and the ffmpeg frame grabber's
pieces (command lines, JPEG quality steps, the MJPEG splitter)."""

import base64
import io
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import timedelta
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
from fastapi import FastAPI
from frames_support import FRONT_CAMERAS, JPEG, FakeGrabber, LiveVendor, jpeg
from mowdeck_contracts.v1 import (
    ConnectionKind,
    MowerActivity,
    MowerState,
    PrincipalKind,
    PrincipalRef,
    VisionFrameSetReply,
)
from mowdeck_contracts.v1.subjects import telemetry_subject
from mowdeck_service_kit.bus import InMemoryBus
from mowdeck_service_kit.testing import lifespan_client
from PIL import Image
from relay_support import T0, FakeClock, FakeMedia

from camera_relay.adapters.bus.telemetry import mower_status
from camera_relay.adapters.media.frames import (
    QUALITY_STEPS,
    FfmpegFrameGrabber,
    encode_jpeg,
    snapshot_args,
    split_jpegs,
    stream_args,
)
from camera_relay.application.frames import Frame, FrameTooLargeError
from camera_relay.application.ports import UnsupportedProviderError
from camera_relay.main import build_app
from camera_relay.settings import Settings

TOKEN = "frames-test-token"  # noqa: S105 - test value
AUTH = {"Authorization": f"Bearer {TOKEN}"}


def _settings(**overrides: object) -> Settings:
    return Settings.model_validate(
        {
            "environment": "test",
            "log_format": "console",
            "relay_frames_token": TOKEN,
            **overrides,
        }
    )


def _state(mower_id: UUID, account_id: UUID, **overrides: Any) -> MowerState:
    values: dict[str, Any] = {
        "mower_id": mower_id,
        "account_id": account_id,
        "observed_at": T0,
        "heartbeat_at": T0,
        "activity": MowerActivity.DOCKED,
        "battery_percent": 80,
        "connection": ConnectionKind.SIMULATED,
    }
    values.update(overrides)
    return MowerState(**values)


@asynccontextmanager
async def _app(
    grabber: FakeGrabber | None = None, **settings: object
) -> AsyncIterator[tuple[FastAPI, httpx.AsyncClient, InMemoryBus, LiveVendor]]:
    bus = InMemoryBus()
    vendor = LiveVendor(cameras=FRONT_CAMERAS)
    app = build_app(
        _settings(**settings),
        bus=bus,
        clock=FakeClock(),
        vendor=vendor,
        media=FakeMedia(),
        frame_grabber=grabber or FakeGrabber(),
        background_tasks=False,
    )
    vendor.service = app.state.relay
    async with lifespan_client(app) as client:
        yield app, client, bus, vendor


def _body(mower_id: UUID, account_id: UUID, **extra: Any) -> dict[str, Any]:
    return {
        "account_id": str(account_id),
        "mower_id": str(mower_id),
        "max_width": 640,
        "max_bytes": 200_000,
        "timeout_s": 5,
        **extra,
    }


async def test_a_snapshot_answers_a_jpeg_with_the_frame_times() -> None:
    mower, account = uuid4(), uuid4()
    async with _app() as (_, client, bus, vendor):
        await bus.publish(telemetry_subject(mower), _state(mower, account))
        response = await client.post(
            "/internal/v1/frames/snapshot", json=_body(mower, account), headers=AUTH
        )
        assert response.status_code == 200, response.text
        assert response.headers["content-type"] == "image/jpeg"
        assert response.content == JPEG
        assert response.headers["x-frame-received-at"].startswith("20")
        assert response.headers["x-frame-captured-at"]
        assert (response.headers["x-frame-width"], response.headers["x-frame-height"]) == (
            "640",
            "480",
        )
        assert len(vendor.starts) == 1


async def test_the_frame_tap_needs_its_token() -> None:
    mower, account = uuid4(), uuid4()
    async with _app() as (_, client, _bus, _vendor):
        missing = await client.post("/internal/v1/frames/snapshot", json=_body(mower, account))
        assert missing.status_code == 401
        wrong = await client.post(
            "/internal/v1/frames/snapshot",
            json=_body(mower, account),
            headers={"Authorization": "Bearer nope"},
        )
        assert wrong.status_code == 401
        stream = await client.get(
            "/internal/v1/frames/stream",
            params={"account_id": str(account), "mower_id": str(mower)},
        )
        assert stream.status_code == 401


async def test_without_a_token_the_frame_tap_is_off() -> None:
    mower, account = uuid4(), uuid4()
    async with _app(relay_frames_token=None) as (_, client, _bus, _vendor):
        response = await client.post(
            "/internal/v1/frames/snapshot", json=_body(mower, account), headers=AUTH
        )
        assert response.status_code == 503
        assert response.json()["type"].endswith(":frames-disabled")


@pytest.mark.parametrize(
    ("state", "status", "problem"),
    [
        ({"battery_percent": 9}, 409, "camera-budget"),
        ({"activity": MowerActivity.OFFLINE}, 503, "camera-unavailable"),
        ({"connected": False}, 503, "camera-unavailable"),
        ({"heartbeat_at": T0 - timedelta(minutes=10)}, 503, "camera-unavailable"),
    ],
)
async def test_refusals_are_problem_documents(
    state: dict[str, Any], status: int, problem: str
) -> None:
    mower, account = uuid4(), uuid4()
    async with _app() as (_, client, bus, vendor):
        await bus.publish(telemetry_subject(mower), _state(mower, account, **state))
        response = await client.post(
            "/internal/v1/frames/snapshot", json=_body(mower, account), headers=AUTH
        )
        assert response.status_code == status, response.text
        assert response.json()["type"].endswith(f":{problem}")
        assert vendor.starts == []


async def test_an_unknown_mower_state_is_unavailable() -> None:
    async with _app() as (_, client, _bus, _vendor):
        response = await client.post(
            "/internal/v1/frames/snapshot", json=_body(uuid4(), uuid4()), headers=AUTH
        )
        assert response.status_code == 503
        assert "never wakes" in response.json()["detail"]


async def test_another_account_gets_404_and_no_frame() -> None:
    mower, owner, other = uuid4(), uuid4(), uuid4()
    grabber = FakeGrabber()
    async with _app(grabber) as (_, client, bus, vendor):
        vendor.owner[mower] = owner
        await bus.publish(telemetry_subject(mower), _state(mower, owner))
        ok = await client.post(
            "/internal/v1/frames/snapshot", json=_body(mower, owner), headers=AUTH
        )
        assert ok.status_code == 200
        refused = await client.post(
            "/internal/v1/frames/snapshot", json=_body(mower, other), headers=AUTH
        )
        assert refused.status_code == 404
        assert len(grabber.snapshots) == 1


async def test_an_unsupported_provider_is_its_own_problem() -> None:
    mower, account = uuid4(), uuid4()
    async with _app() as (_, client, bus, vendor):
        vendor.offer_fails_with = UnsupportedProviderError("no driver for agora")
        await bus.publish(telemetry_subject(mower), _state(mower, account))
        response = await client.post(
            "/internal/v1/frames/snapshot", json=_body(mower, account), headers=AUTH
        )
        assert response.status_code == 503
        assert response.json()["type"].endswith(":camera-relay-unsupported")


async def test_a_user_principal_cannot_own_a_frame_lease() -> None:
    mower, account = uuid4(), uuid4()
    async with _app() as (_, client, _bus, _vendor):
        body = _body(mower, account, requested_by={"subject": str(uuid4()), "kind": "user"})
        response = await client.post("/internal/v1/frames/snapshot", json=body, headers=AUTH)
        assert response.status_code == 422


async def test_a_stream_is_multipart_jpeg_with_frame_times() -> None:
    mower, account = uuid4(), uuid4()
    grabber = FakeGrabber(stream_frames=3)
    async with _app(grabber) as (app, client, bus, _vendor):

        async def offline() -> None:  # ends the stream after three frames
            await bus.publish(
                telemetry_subject(mower), _state(mower, account, activity=MowerActivity.OFFLINE)
            )

        grabber.after_stream = offline
        await bus.publish(telemetry_subject(mower), _state(mower, account))
        response = await client.get(
            "/internal/v1/frames/stream",
            params={"account_id": str(account), "mower_id": str(mower), "fps": 2},
            headers=AUTH,
        )
        assert response.status_code == 200
        assert response.headers["content-type"] == "multipart/x-mixed-replace; boundary=frame"
        assert response.headers["x-frame-camera"] == "front_left"
        parts = response.content.split(b"--frame\r\n")[1:]
        assert len(parts) == 3
        for index, part in enumerate(parts):
            head, _, body = part.partition(b"\r\n\r\n")
            headers = dict(line.split(": ", 1) for line in head.decode().split("\r\n"))
            assert headers["Content-Type"] == "image/jpeg"
            assert headers["X-Frame-Index"] == str(index)
            assert "X-Frame-Received-At" in headers and "X-Frame-Captured-At" in headers
            assert body.removesuffix(b"\r\n") == JPEG
            assert int(headers["Content-Length"]) == len(JPEG)
        assert grabber.streams[0][1] == 2
        sessions = app.state.relay.repository
        assert all(not s.leases for k in sessions.session_keys() if (s := sessions.get(k)))


async def test_a_stream_rate_above_five_is_refused() -> None:
    async with _app() as (_, client, _bus, _vendor):
        response = await client.get(
            "/internal/v1/frames/stream",
            params={"account_id": str(uuid4()), "mower_id": str(uuid4()), "fps": 10},
            headers=AUTH,
        )
        assert response.status_code == 422


async def test_without_a_media_plane_frames_are_unavailable() -> None:
    bus = InMemoryBus()
    app = build_app(_settings(), bus=bus, background_tasks=False)
    async with lifespan_client(app) as client:
        response = await client.post(
            "/internal/v1/frames/snapshot", json=_body(uuid4(), uuid4()), headers=AUTH
        )
        assert response.status_code == 503
        assert response.json()["type"].endswith(":camera-unavailable")


# --- Mower states -------------------------------------------------------------------------------


def test_a_state_becomes_the_relays_mower_status() -> None:
    mower, account = uuid4(), uuid4()
    status = mower_status(_state(mower, account, battery_percent=42), T0)
    assert (status.online, status.asleep, status.battery_percent, status.charging) == (
        True,
        False,
        42.0,
        False,
    )
    charging = mower_status(_state(mower, account, activity=MowerActivity.CHARGING), T0)
    assert charging.charging
    asleep = mower_status(_state(mower, account, activity=MowerActivity.OFFLINE), T0)
    assert asleep.asleep and not asleep.reachable
    docked = mower_status(_state(mower, account, activity=MowerActivity.DOCKED), T0)
    assert docked.online and docked.reachable and not docked.asleep
    gone = mower_status(_state(mower, account, connected=False), T0)
    assert not gone.online
    stale = mower_status(_state(mower, account), T0 + timedelta(minutes=3))
    assert not stale.online


async def test_states_keep_the_video_budget_of_the_last_offer() -> None:
    mower, account = uuid4(), uuid4()
    async with _app() as (app, client, bus, vendor):
        vendor.quota_remaining_percent = 55.0
        await bus.publish(telemetry_subject(mower), _state(mower, account))
        response = await client.post(
            "/internal/v1/frames/snapshot", json=_body(mower, account), headers=AUTH
        )
        assert response.status_code == 200
        await bus.publish(telemetry_subject(mower), _state(mower, account, battery_percent=70))
        status = app.state.relay.repository.status_of(mower)
        assert status.battery_percent == 70
        assert status.quota_remaining_percent == 55.0


# --- ffmpeg grabber pieces ----------------------------------------------------------------------


def test_the_snapshot_command_reads_one_frame_as_png_without_upscaling() -> None:
    args = snapshot_args(
        ffmpeg="ffmpeg", input_url="rtsp://u:s@m:8554/r/x/front_left", max_width=640
    )
    assert args[args.index("-frames:v") + 1] == "1"
    assert args[args.index("-vf") + 1] == "scale=w='min(640,iw)':h=-2"
    assert args[args.index("-c:v") + 1] == "png"
    assert args[args.index("-rtsp_transport") + 1] == "tcp"
    assert args[-1] == "pipe:1"


def test_the_stream_command_drops_frames_and_never_repeats_them() -> None:
    args = stream_args(
        ffmpeg="ffmpeg", input_url="rtsp://m/r/x/c", fps=4, max_width=320, duration=12.5
    )
    vf = args[args.index("-vf") + 1]
    assert vf.startswith("select='isnan(prev_selected_t)+gte(t-prev_selected_t,0.250)'")
    assert vf.endswith("scale=w='min(320,iw)':h=-2")
    assert args[args.index("-fps_mode") + 1] == "vfr"
    assert args[args.index("-t") + 1] == "12.500"
    assert args[args.index("-c:v") + 1] == "mjpeg"
    assert "fps=" not in vf


def test_jpeg_quality_steps_down_to_the_size_cap() -> None:
    noisy = Image.effect_noise((640, 480), 90).convert("RGB")
    full, quality = encode_jpeg(noisy, max_bytes=10_000_000)
    assert quality == pytest.approx(0.8)
    smaller, lower = encode_jpeg(noisy, max_bytes=len(full) - 1)
    assert lower < quality and len(smaller) < len(full)
    assert lower >= 0.8 * QUALITY_STEPS[-1]
    with pytest.raises(FrameTooLargeError):
        encode_jpeg(noisy, max_bytes=2_000)
    with Image.open(io.BytesIO(smaller)) as image:
        assert image.size == (640, 480)


async def test_a_stream_frame_above_the_cap_is_fitted_with_the_quality_steps() -> None:
    noisy = Image.effect_noise((640, 480), 90).convert("RGB")
    buffer = io.BytesIO()
    noisy.save(buffer, format="JPEG", quality=95)
    big = Frame(data=buffer.getvalue(), received_at=T0, captured_at=T0, width=640, height=480)
    grabber = FfmpegFrameGrabber(server=None)  # type: ignore[arg-type] # fit needs no server
    assert await grabber.fit(big, max_bytes=len(big.data)) is big
    fitted = await grabber.fit(big, max_bytes=len(big.data) - 1)
    assert len(fitted.data) < len(big.data)
    assert (fitted.captured_at, fitted.width) == (T0, 640)
    with pytest.raises(FrameTooLargeError):
        await grabber.fit(big, max_bytes=2_000)
    with pytest.raises(FrameTooLargeError):
        await grabber.fit(replace(big, data=b"not a jpeg"), max_bytes=5)


def test_the_mjpeg_splitter_cuts_complete_images_only() -> None:
    one, two = jpeg(32, 24), jpeg(16, 12)
    buffer = bytearray(b"junk" + one + two[:10])
    assert split_jpegs(buffer) == [one]
    assert bytes(buffer) == two[:10]
    buffer.extend(two[10:])
    assert split_jpegs(buffer) == [two]
    assert buffer == bytearray()


async def test_the_demand_decides_whether_a_sleeping_mower_is_woken() -> None:
    mower, account = uuid4(), uuid4()
    async with _app() as (_, client, bus, _vendor):
        await bus.publish(
            telemetry_subject(mower), _state(mower, account, activity=MowerActivity.OFFLINE)
        )
        routine = await client.post(
            "/internal/v1/frames/snapshot", json=_body(mower, account), headers=AUTH
        )
        assert routine.status_code == 503
        woken = await client.post(
            "/internal/v1/frames/snapshot",
            json=_body(mower, account, demand="error_analysis"),
            headers=AUTH,
        )
        assert woken.status_code == 200, woken.text


# --- Frame sets -----------------------------------------------------------------------------


async def test_the_set_route_answers_both_frames_as_json() -> None:
    mower, account = uuid4(), uuid4()
    async with _app(FakeGrabber()) as (_, client, bus, vendor):
        await bus.publish(telemetry_subject(mower), _state(mower, account))
        response = await client.post(
            "/internal/v1/frames/set",
            json=_body(mower, account, demand="error_analysis"),
            headers=AUTH,
        )
        assert response.status_code == 200, response.text
        assert response.headers["content-type"] == "application/json"
        reply = VisionFrameSetReply.model_validate_json(response.content)
        assert [(f.camera_id, f.side, f.camera_name) for f in reply.frames] == [
            ("front_left", "left", "Front left"),
            ("front_right", "right", "Front right"),
        ]
        assert all(base64.b64decode(f.image_base64) == JPEG for f in reply.frames)
        assert reply.complete
        assert reply.skew_ms is not None and reply.skew_ms < 200
        assert [start.camera_ids for start in vendor.starts] == [("front_left", "front_right")]


async def test_the_set_route_needs_its_token_and_a_service_principal() -> None:
    mower, account = uuid4(), uuid4()
    async with _app() as (_, client, bus, _vendor):
        await bus.publish(telemetry_subject(mower), _state(mower, account))
        missing = await client.post("/internal/v1/frames/set", json=_body(mower, account))
        assert missing.status_code == 401
        user = PrincipalRef(subject=uuid4(), kind=PrincipalKind.USER)
        refused = await client.post(
            "/internal/v1/frames/set",
            json=_body(mower, account, requested_by=user.model_dump(mode="json")),
            headers=AUTH,
        )
        assert refused.status_code == 422
        too_many = await client.post(
            "/internal/v1/frames/set", json=_body(mower, account, max_frames=3), headers=AUTH
        )
        assert too_many.status_code == 422


async def test_the_set_route_refuses_another_account_with_404() -> None:
    mower, account = uuid4(), uuid4()
    async with _app() as (_, client, bus, vendor):
        vendor.owner[mower] = account
        await bus.publish(telemetry_subject(mower), _state(mower, account))
        response = await client.post(
            "/internal/v1/frames/set", json=_body(mower, uuid4()), headers=AUTH
        )
        assert response.status_code == 404
        assert vendor.starts == []
