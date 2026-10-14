"""The capture worker (``python -m vision.capture``, ADR 0035): API, limits, the page script.

The API runs with fake grabbers. ``test_vision_capture_browser_*`` runs the real
``PlaywrightGrabber`` in headless Chromium against a stub of the Agora Web SDK (a canvas stream
instead of a camera) when ``MOWDECK_TEST_CAPTURE_BROWSER=1`` and Playwright with its Chromium
are installed (``uv run --extra capture`` / ``--with playwright``, then
``python -m playwright install chromium-headless-shell``).
"""

# ruff: noqa: S105, S106 - placeholder tokens, not credentials

import asyncio
import io
import os
from pathlib import Path
from typing import Any

import pytest
from mowdeck_contracts.v1 import (
    CameraEncryption,
    CameraInfo,
    CameraProvider,
    CameraSession,
    CameraSnapshotRequest,
    utc_now,
)
from mowdeck_service_kit.testing import lifespan_client
from PIL import Image

from vision.capture.app import build_capture_app, grabber_from
from vision.capture.grabber import (
    ORIGIN,
    CaptureFailed,
    FakeGrabber,
    PlaywrightGrabber,
    _arguments,
    _reason,
)
from vision.capture.settings import CaptureSettings

TOKEN = "capture-secret"


def _settings(**overrides: Any) -> CaptureSettings:
    values: dict[str, Any] = {
        "environment": "test",
        "log_format": "console",
        "capture_backend": "fake",
        "capture_token": TOKEN,
        **overrides,
    }
    return CaptureSettings.model_validate(values)


def _session(**changes: Any) -> CameraSession:
    values: dict[str, Any] = {
        "provider": CameraProvider.AGORA,
        "app_id": "app-id",
        "channel": "channel-x",
        "token": "agora-token",
        "uid": 4242,
        "cameras": [CameraInfo(id="0", name="Front left", uid=1)],
        "expires_at": utc_now(),
    }
    values.update(changes)
    return CameraSession.model_validate(values)


def _body(**changes: Any) -> dict[str, Any]:
    request = CameraSnapshotRequest(session=_session(), camera_uid=1, timeout_seconds=5)
    data: dict[str, Any] = request.model_dump(mode="json")
    data.update(changes)
    return data


AUTH = {"Authorization": f"Bearer {TOKEN}"}


class TimeoutGrabber(FakeGrabber):
    async def grab(self, request: CameraSnapshotRequest, *, quality: float) -> bytes:
        raise TimeoutError


class SizedGrabber(FakeGrabber):
    """Large at high quality, small below 0.5."""

    def __init__(self) -> None:
        super().__init__()
        self.qualities: list[float] = []

    async def grab(self, request: CameraSnapshotRequest, *, quality: float) -> bytes:
        self.qualities.append(quality)
        return b"\xff\xd8" + b"x" * (5000 if quality >= 0.5 else 500)


async def test_vision_capture_api_returns_a_jpeg_with_the_token() -> None:
    grabber = FakeGrabber()
    app = build_capture_app(_settings(), grabber=grabber)
    async with lifespan_client(app) as client:
        refused = await client.post("/api/v1/capture", json=_body())
        wrong = await client.post(
            "/api/v1/capture", json=_body(), headers={"Authorization": "Bearer nope"}
        )
        ok = await client.post("/api/v1/capture", json=_body(), headers=AUTH)
        health = await client.get("/healthz")
    assert refused.status_code == 401 and wrong.status_code == 401
    assert ok.status_code == 200, ok.text
    assert ok.headers["content-type"] == "image/jpeg"
    assert ok.content[:2] == b"\xff\xd8"
    assert health.status_code == 200
    [request] = grabber.requests
    assert request.camera_uid == 1 and request.session.token == "agora-token"


async def test_vision_capture_api_maps_failures_and_timeouts() -> None:
    async with lifespan_client(
        build_capture_app(_settings(), grabber=FakeGrabber(fail="no-publisher"))
    ) as client:
        failed = await client.post("/api/v1/capture", json=_body(), headers=AUTH)
    assert failed.status_code == 502
    assert failed.json()["type"].endswith("capture-failed")
    assert "no-publisher" in failed.json()["detail"]
    async with lifespan_client(build_capture_app(_settings(), grabber=TimeoutGrabber())) as client:
        late = await client.post("/api/v1/capture", json=_body(), headers=AUTH)
    assert late.status_code == 504
    assert late.json()["type"].endswith("capture-timeout")


async def test_vision_capture_api_limits_concurrency() -> None:
    settings = _settings(capture_max_concurrency=1, capture_queue_seconds=0.05, capture_token=None)
    app = build_capture_app(settings, grabber=FakeGrabber(delay=0.4))
    async with lifespan_client(app) as client:
        first, second = await asyncio.gather(
            client.post("/api/v1/capture", json=_body()),
            client.post("/api/v1/capture", json=_body()),
        )
    statuses = sorted([first.status_code, second.status_code])
    assert statuses == [200, 503]
    busy = first if first.status_code == 503 else second
    assert busy.headers["retry-after"] == "5"


async def test_vision_capture_api_lowers_the_quality_for_the_size_cap() -> None:
    grabber = SizedGrabber()
    async with lifespan_client(build_capture_app(_settings(), grabber=grabber)) as client:
        ok = await client.post("/api/v1/capture", json=_body(max_bytes=1024), headers=AUTH)
    assert ok.status_code == 200 and len(ok.content) < 1024
    assert grabber.qualities == pytest.approx([0.8, 0.6, 0.4])
    grabber = SizedGrabber()
    async with lifespan_client(
        build_capture_app(_settings(capture_jpeg_quality=1.0), grabber=grabber)
    ) as client:
        too_large = await client.post("/api/v1/capture", json=_body(max_bytes=1024), headers=AUTH)
    assert too_large.status_code == 502 and "max_bytes" in too_large.json()["detail"]


async def test_vision_capture_api_validates_the_request() -> None:
    async with lifespan_client(build_capture_app(_settings(), grabber=FakeGrabber())) as client:
        invalid = await client.post("/api/v1/capture", json={"camera_uid": 1}, headers=AUTH)
    assert invalid.status_code == 422


def test_vision_capture_page_arguments_and_reasons() -> None:
    session = _session(
        encryption=CameraEncryption(mode="aes-256-gcm2", key="k" * 32, salt="c2FsdA==")
    )
    request = CameraSnapshotRequest(session=session, camera_uid=None, timeout_seconds=7.5)
    arguments = _arguments(request, quality=0.7, settle_ms=300, profile="rtc")
    assert arguments["appId"] == "app-id" and arguments["uid"] == 4242
    assert arguments["cameraUid"] is None and arguments["timeoutMs"] == 7500
    assert arguments["encryption"] == {"mode": "aes-256-gcm2", "key": "k" * 32, "salt": "c2FsdA=="}
    assert arguments["quality"] == 0.7 and arguments["settleMs"] == 300
    assert (
        _arguments(
            CameraSnapshotRequest(session=_session()), quality=1, settle_ms=0, profile="live"
        )["encryption"]
        is None
    )
    assert _reason(Exception("Error: capture:no-frame at ...")) == "no-frame"
    assert _reason(Exception("Error: capture:no-publisher")) == "no-publisher"
    assert _reason(Exception("Target closed")) == "browser-error"
    agora = Exception("AgoraRTCError CAN_NOT_GET_GATEWAY_SERVER: invalid vendor key, token 007x")
    assert _reason(agora) == "agora:CAN_NOT_GET_GATEWAY_SERVER"
    assert CaptureFailed("x").reason == "x"


def test_vision_capture_grabber_choice_and_page() -> None:
    assert isinstance(grabber_from(_settings()), FakeGrabber)
    real = grabber_from(_settings(capture_backend="playwright", capture_browser_channel="chrome"))
    assert isinstance(real, PlaywrightGrabber) and real.channel == "chrome"
    local = PlaywrightGrabber(sdk="/opt/agora/AgoraRTC_N-production.js")
    assert f'src="{ORIGIN}/agora.js"' in local._page_html()
    cdn = PlaywrightGrabber(sdk="https://cdn.example/agora.js")
    assert 'src="https://cdn.example/agora.js"' in cdn._page_html()


# --- the real browser (opt-in) -------------------------------------------------------------

STUB_SDK = """
globalThis.AgoraRTC = {
  setLogLevel() {}, disableLogUpload() {}, onAutoplayFailed: null,
  createClient() {
    const handlers = {};
    const canvas = document.createElement('canvas');
    canvas.width = 320; canvas.height = 240;
    const ctx = canvas.getContext('2d');
    const paint = () => { ctx.fillStyle = '#3a7a3a'; ctx.fillRect(0, 0, 320, 240); };
    paint(); setInterval(paint, 40);
    const stream = canvas.captureStream(25);
    const user = { uid: 1, hasVideo: true };
    const client = {
      remoteUsers: [],
      encryption: null,
      on(name, fn) { handlers[name] = fn; },
      setEncryptionConfig(mode, key, salt) { client.encryption = [mode, key, salt.length]; },
      async setClientRole() {},
      async join(appId, channel, token, uid) {
        if (token !== 'agora-token') throw new Error('bad token');
        const publish = () => handlers['user-published'](user, 'video');
        setTimeout(publish, 150);
        return uid;
      },
      async subscribe() { return { getMediaStreamTrack: () => stream.getVideoTracks()[0] }; },
      async leave() {},
    };
    return client;
  },
};
"""


def _browser_ready() -> bool:
    if os.environ.get("MOWDECK_TEST_CAPTURE_BROWSER") != "1":
        return False
    try:
        import playwright  # noqa: F401
    except ImportError:
        return False
    return True


@pytest.mark.skipif(not _browser_ready(), reason="needs MOWDECK_TEST_CAPTURE_BROWSER=1")
async def test_vision_capture_browser_grabs_a_frame_from_the_stub_sdk(tmp_path: Path) -> None:
    sdk = tmp_path / "agora.js"
    sdk.write_text(STUB_SDK, encoding="utf-8")
    grabber = PlaywrightGrabber(sdk=str(sdk), settle_ms=100)
    session = _session(
        encryption=CameraEncryption(mode="aes-256-gcm2", key="k" * 32, salt="c2FsdA==")
    )
    try:
        data = await grabber.grab(
            CameraSnapshotRequest(session=session, camera_uid=1, timeout_seconds=10, max_width=160),
            quality=0.8,
        )
        assert data[:2] == b"\xff\xd8"
        image = Image.open(io.BytesIO(data))
        assert image.size == (160, 120)
        pixel = image.convert("RGB").getpixel((80, 60))
        assert isinstance(pixel, tuple)
        red, green, blue = pixel[:3]
        assert green > red and green > blue, "the stub's green picture"
        with pytest.raises(CaptureFailed) as missing:
            await grabber.grab(
                CameraSnapshotRequest(session=session, camera_uid=2, timeout_seconds=1),
                quality=0.8,
            )
        assert missing.value.reason == "no-publisher"
        with pytest.raises(CaptureFailed) as refused:
            await grabber.grab(
                CameraSnapshotRequest(session=_session(token="wrong"), timeout_seconds=1),
                quality=0.8,
            )
        assert refused.value.reason == "browser-error"
    finally:
        await grabber.close()
