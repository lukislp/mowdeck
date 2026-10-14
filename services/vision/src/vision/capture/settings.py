"""Settings of the capture worker (``MOWDECK_CAPTURE_*``; secrets also as ``_FILE``)."""

from typing import Literal

from mowdeck_service_kit import ServiceSettings
from pydantic import Field, SecretStr


class CaptureSettings(ServiceSettings):
    """The kit settings (without NATS) plus the worker's own."""

    service_name: str = Field(
        default="vision-capture", min_length=1, max_length=64, pattern=r"^[a-z][a-z0-9\-]*$"
    )
    """Name of the worker in logs and metrics."""

    nats_enabled: bool = False
    """The worker needs no bus."""

    capture_token: SecretStr | None = None
    """Shared secret the Device-Gateway sends as ``Authorization: Bearer <token>``. Empty: no
    check (only inside a private network)."""

    capture_backend: Literal["playwright", "fake"] = "playwright"
    """``playwright`` (headless Chromium with the Agora Web SDK) or ``fake`` (a generated test
    picture; development and tests without a browser)."""

    capture_max_concurrency: int = Field(default=2, ge=1, le=16)
    """Captures at the same time (each one is a browser page with a video decoder)."""

    capture_queue_seconds: float = Field(default=10.0, ge=0, le=120)
    """How long a request waits for a free slot before ``503``."""

    capture_max_timeout_seconds: float = Field(default=30.0, gt=0, le=120)
    """Upper bound of a request's ``timeout_seconds``."""

    capture_jpeg_quality: float = Field(default=0.8, gt=0, le=1)
    """JPEG quality of the frame (the canvas encoder, 0 to 1)."""

    capture_settle_ms: int = Field(default=400, ge=0, le=5000)
    """Wait after the first decoded frame, so the picture is a complete key frame."""

    capture_agora_sdk: str = "/opt/agora/AgoraRTC_N-production.js"
    """The Agora Web SDK: a local file (the image downloads the pinned version at build time)
    or an ``https://`` URL."""

    capture_browser_channel: str | None = None
    """Playwright browser channel: empty for the bundled Chromium, ``chrome`` for Google Chrome
    (which also decodes H.264 when the camera's stream needs it)."""

    capture_agora_profile: Literal["rtc", "live"] = "rtc"
    """Agora channel profile the worker joins with (as the web app's default)."""
