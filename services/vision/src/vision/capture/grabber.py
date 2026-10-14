"""Frame grabbers: one still frame of a live camera session.

:class:`PlaywrightGrabber` joins the session's Agora channel with the Agora Web SDK in a
headless Chromium (Playwright), as the web app's camera panel does (same encryption handling,
``rtc`` profile, subscribe only), subscribes to the requested camera's video, waits until the
``<video>`` element has a decoded frame, waits ``settle_ms`` more for a complete key frame,
draws it into a canvas (scaled to ``max_width``) and returns the JPEG. Every capture gets a new
browser context (no state between mowers); the browser itself stays up between captures.

The page is served from a reserved ``https://capture.mowdeck.invalid`` origin through request
interception, so it is a secure context and the SDK loads from the local file without any
network request besides Agora's own. The token and the encryption key exist only in the
evaluated arguments; nothing is logged.

:class:`FakeGrabber` returns a generated picture (development and tests without a browser).
"""

import asyncio
import base64
import io
import re
from pathlib import Path
from typing import Any, Protocol

from mowdeck_contracts.v1 import CameraSnapshotRequest
from mowdeck_service_kit import get_logger

_log = get_logger("vision.capture.grabber")

ORIGIN = "https://capture.mowdeck.invalid"
SDK_PATH = "/agora.js"

PAGE = """<!doctype html>
<html><head><meta charset="utf-8"><title>capture</title>
<script src="{sdk}"></script></head><body></body></html>"""

CAPTURE_JS = """
async (a) => {
  const AgoraRTC = globalThis.AgoraRTC;
  if (!AgoraRTC) throw new Error('capture:no-sdk');
  AgoraRTC.setLogLevel(3);
  AgoraRTC.disableLogUpload();
  AgoraRTC.onAutoplayFailed = () => undefined;
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  const deadline = Date.now() + a.timeoutMs;
  const left = () => Math.max(0, deadline - Date.now());
  const wanted = (u) => u.hasVideo && (a.cameraUid === null || Number(u.uid) === a.cameraUid);
  const client = AgoraRTC.createClient({ mode: a.profile, codec: 'vp8' });
  if (a.encryption) {
    const salt = a.encryption.salt
      ? Uint8Array.from(atob(a.encryption.salt), (c) => c.charCodeAt(0))
      : undefined;
    client.setEncryptionConfig(a.encryption.mode, a.encryption.key, salt);
  }
  let found = null;
  const published = new Promise((resolve) => {
    client.on('user-published', (user, mediaType) => {
      if (mediaType === 'video' && wanted(user)) resolve(user);
    });
  });
  try {
    if (a.profile === 'live') await client.setClientRole('audience', { level: 1 });
    await client.join(a.appId, a.channel, a.token || null, a.uid);
    found = client.remoteUsers.find(wanted) || null;
    if (!found) found = await Promise.race([published, sleep(left()).then(() => null)]);
    if (!found) throw new Error('capture:no-publisher');
    const track = await client.subscribe(found, 'video');
    const video = document.createElement('video');
    video.muted = true;
    video.playsInline = true;
    video.autoplay = true;
    video.srcObject = new MediaStream([track.getMediaStreamTrack()]);
    document.body.appendChild(video);
    video.play().catch(() => undefined);
    while (left() > 0 && !(video.videoWidth > 0 && video.readyState >= 2)) await sleep(100);
    if (!(video.videoWidth > 0)) throw new Error('capture:no-frame');
    await sleep(Math.min(a.settleMs, left()));
    const scale = Math.min(1, a.maxWidth / video.videoWidth);
    const canvas = document.createElement('canvas');
    canvas.width = Math.max(1, Math.round(video.videoWidth * scale));
    canvas.height = Math.max(1, Math.round(video.videoHeight * scale));
    canvas.getContext('2d').drawImage(video, 0, 0, canvas.width, canvas.height);
    const url = canvas.toDataURL('image/jpeg', a.quality);
    return { data: url.slice(url.indexOf(',') + 1), width: canvas.width, height: canvas.height,
             uid: Number(found.uid) };
  } finally {
    try { await client.leave(); } catch (e) { /* already gone */ }
  }
}
"""


class CaptureFailed(Exception):  # noqa: N818 - reads as an outcome
    """No frame could be taken; ``reason`` is safe to return (never the token)."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class FrameGrabber(Protocol):
    """Takes one JPEG frame of a camera session."""

    async def grab(self, request: CameraSnapshotRequest, *, quality: float) -> bytes:
        """The JPEG; raises :class:`CaptureFailed` or ``TimeoutError``."""
        ...

    async def close(self) -> None:
        """Release the browser."""
        ...


def _arguments(
    request: CameraSnapshotRequest, *, quality: float, settle_ms: int, profile: str
) -> dict[str, Any]:
    session = request.session
    encryption = None
    if session.encryption is not None:
        encryption = {
            "mode": session.encryption.mode,
            "key": session.encryption.key,
            "salt": session.encryption.salt,
        }
    return {
        "appId": session.app_id,
        "channel": session.channel,
        "token": session.token,
        "uid": session.uid,
        "cameraUid": request.camera_uid,
        "encryption": encryption,
        "timeoutMs": int(request.timeout_seconds * 1000),
        "settleMs": settle_ms,
        "maxWidth": request.max_width,
        "quality": quality,
        "profile": profile,
    }


_AGORA_ERROR = re.compile(r"AgoraRTCError ([A-Z_]{3,64})")


def _reason(exc: Exception) -> str:
    """A short, secret free reason: ours, Agora's error code, or ``browser-error``."""
    text = str(exc)
    for reason in ("no-sdk", "no-publisher", "no-frame"):
        if f"capture:{reason}" in text:
            return reason
    agora = _AGORA_ERROR.search(text)
    if agora is not None:
        return f"agora:{agora.group(1)}"
    return "browser-error"


class PlaywrightGrabber:
    """Headless Chromium with the Agora Web SDK (Playwright is imported lazily)."""

    def __init__(
        self,
        *,
        sdk: str,
        channel: str | None = None,
        settle_ms: int = 400,
        profile: str = "rtc",
    ) -> None:
        self.sdk = sdk
        self.channel = channel or None
        self.settle_ms = settle_ms
        self.profile = profile
        self._playwright: Any = None
        self._browser: Any = None
        self._lock = asyncio.Lock()
        self._sdk_body: bytes | None = None

    def _page_html(self) -> str:
        src = self.sdk if self.sdk.startswith("https://") else ORIGIN + SDK_PATH
        return PAGE.format(sdk=src)

    def _sdk_bytes(self) -> bytes:
        if self._sdk_body is None:
            self._sdk_body = Path(self.sdk).read_bytes()
        return self._sdk_body

    async def _ensure_browser(self) -> Any:
        async with self._lock:
            if self._browser is not None and self._browser.is_connected():
                return self._browser
            from playwright.async_api import async_playwright

            if self._playwright is None:
                self._playwright = await async_playwright().start()
            self._browser = await self._playwright.chromium.launch(
                headless=True,
                channel=self.channel,
                args=[
                    "--autoplay-policy=no-user-gesture-required",
                    "--disable-dev-shm-usage",
                    "--mute-audio",
                ],
            )
            _log.info("browser started", channel=self.channel or "chromium")
            return self._browser

    async def grab(self, request: CameraSnapshotRequest, *, quality: float) -> bytes:
        """Join, subscribe, wait for a frame, return it as JPEG."""
        browser = await self._ensure_browser()
        context = await browser.new_context(viewport={"width": 640, "height": 480})
        try:
            page = await context.new_page()
            html = self._page_html()

            async def serve(route: Any) -> None:
                url: str = route.request.url
                if url.rstrip("/") == ORIGIN:
                    await route.fulfill(status=200, content_type="text/html", body=html)
                elif url == ORIGIN + SDK_PATH:
                    await route.fulfill(
                        status=200, content_type="text/javascript", body=self._sdk_bytes()
                    )
                else:
                    await route.fulfill(status=404, body="")

            await page.route(f"{ORIGIN}/**", serve)
            await page.goto(ORIGIN + "/", wait_until="load")
            arguments = _arguments(
                request, quality=quality, settle_ms=self.settle_ms, profile=self.profile
            )
            try:
                result = await page.evaluate(CAPTURE_JS, arguments)
            except Exception as exc:  # Playwright's Error carries the page's message
                raise CaptureFailed(_reason(exc)) from None
            return base64.b64decode(result["data"])
        finally:
            await context.close()

    async def close(self) -> None:
        """Close the browser and Playwright."""
        if self._browser is not None:
            await self._browser.close()
            self._browser = None
        if self._playwright is not None:
            await self._playwright.stop()
            self._playwright = None


class FakeGrabber:
    """A generated picture instead of a camera (no browser; development and tests)."""

    def __init__(self, *, fail: str | None = None, delay: float = 0.0) -> None:
        self.fail = fail
        self.delay = delay
        self.requests: list[CameraSnapshotRequest] = []

    async def grab(self, request: CameraSnapshotRequest, *, quality: float) -> bytes:
        """A grey-green gradient as JPEG (Pillow), sized like a scaled camera frame."""
        self.requests.append(request)
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.fail:
            raise CaptureFailed(self.fail)
        from PIL import Image

        width = min(request.max_width, 640)
        height = width * 3 // 4
        image = Image.new("RGB", (width, height))
        image.putdata(
            [
                (60 + x * 80 // width, 110 + y * 60 // height, 50)
                for y in range(height)
                for x in range(width)
            ]
        )
        buffer = io.BytesIO()
        image.save(buffer, format="JPEG", quality=max(1, int(quality * 100)))
        return buffer.getvalue()

    async def close(self) -> None:
        """Nothing to release."""


__all__ = [
    "CAPTURE_JS",
    "CaptureFailed",
    "FakeGrabber",
    "FrameGrabber",
    "PlaywrightGrabber",
]
