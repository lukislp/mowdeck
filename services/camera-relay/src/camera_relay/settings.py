"""Settings of the camera-relay service.

Every field is read from ``MOWDECK_<NAME>`` or from the file named by ``MOWDECK_<NAME>_FILE``.
The field docstrings are the configuration reference (README settings table). The timings and
limits of sessions and leases are the defaults of ADR 0045 (``mowdeck_contracts.v1.relay``) and
not configurable.
"""

from typing import Self
from uuid import UUID

from mowdeck_service_kit import ServiceSettings
from pydantic import Field, SecretStr, model_validator

from camera_relay.application.service import RelayConfig
from camera_relay.domain.policy import MAX_LEASES_PER_SESSION, RelayPolicy

SERVICE_SUBJECT = UUID("5d2c7a8e-3f0b-4c61-9a3e-0c6f1b7d2e45")
"""Default subject of the relay's service principal in gateway requests (stable, not secret)."""


class Settings(ServiceSettings):
    """Settings of camera-relay: the kit settings plus service defaults."""

    service_name: str = Field(
        default="camera-relay", min_length=1, max_length=64, pattern=r"^[a-z][a-z0-9\-]*$"
    )
    """Name of the service in logs, metrics and traces."""

    http_port: int = Field(default=8000, ge=1, le=65535)
    """Port the HTTP server listens on."""

    relay_whep_base_url: str = Field(
        default="/media/whep", min_length=1, max_length=256, pattern=r"^(/|https?://)[^\s]*$"
    )
    """Prefix of the WHEP URLs handed to clients (``<prefix>/r/<relay_id>/<camera>/whep``):
    an absolute path on the MowDeck origin or an ``http(s)`` URL. Traefik routes
    ``/media/whep/r/`` to the media server and strips ``/media/whep``."""

    relay_ticket_signing_key: SecretStr | None = None
    """Ed25519 key that signs the tickets: PEM (PKCS#8) or the base64 encoded 32-byte seed.
    Unset: a new key per process (enough for the single replica, which verifies its own
    tickets)."""

    relay_tick_seconds: float = Field(default=1.0, gt=0, le=10)
    """How often deadlines (heartbeats, grace, start timeout, backoff, Vision limits) are
    processed. Event times are exact regardless; this only bounds the delay."""

    relay_max_leases_per_session: int = Field(default=MAX_LEASES_PER_SESSION, ge=1, le=1000)
    """Open leases per account and mower; one more is answered with ``unavailable``."""

    relay_mediamtx_api_url: str | None = Field(
        default=None, max_length=256, pattern=r"^https?://[^\s]+$"
    )
    """Control API of the media server (MediaMTX), e.g. ``http://mediamtx:9997``. Unset: no
    media plane and no source drivers; every attach is answered with ``unavailable``."""

    relay_mediamtx_rtsp_url: str = Field(
        default="rtsp://mediamtx:8554", max_length=256, pattern=r"^rtsp://[^\s@/]+$"
    )
    """RTSP server of the media server the source pipeline publishes to and reads from
    (inside the cluster only), without credentials."""

    relay_publisher_user: str = Field(
        default="relay", min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_-]+$"
    )
    """User name of the relay's own pipeline towards the media server (publish, internal
    reads, control API)."""

    relay_publisher_secret: SecretStr | None = None
    """Password of that user, usually as ``_FILE``. Unset: a random secret per process (the
    media server asks this process for every publish, so nothing else needs to know it)."""

    relay_reconcile_seconds: float = Field(default=2.0, gt=0, le=30)
    """How often the relay reads the media server's readers (media confirmation, lost
    readers, source readiness)."""

    relay_media_stall_polls: int = Field(default=3, ge=1, le=30)
    """A bound reader whose sent bytes do not grow for this many polls counts as lost."""

    relay_source_ready_seconds: float = Field(default=20.0, ge=5, le=120)
    """How long a camera's ``.../src`` (the vendor stream before transcoding) may take to
    become ready after its first key frame before it is logged as late; it is still awaited
    and its transcoder starts only once it is ready."""

    relay_gateway_timeout_seconds: float = Field(default=10.0, gt=0, le=60)
    """Timeout of the Device-Gateway's camera session and release requests."""

    relay_service_subject: UUID = SERVICE_SUBJECT
    """Subject of the relay's service principal in the gateway requests."""

    relay_ffmpeg_path: str = Field(default="ffmpeg", min_length=1, max_length=256)
    """The ffmpeg binary of the source pipeline (fake source, transcoder)."""

    relay_fake_font: str = Field(
        default="/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf", max_length=256
    )
    """Font of the fake source's overlay (mower, camera, wall clock in ms)."""

    relay_agora_bridge_url: str | None = Field(
        default=None, max_length=256, pattern=r"^(tcp://[^\s/]+:[0-9]{1,5}/?)?$"
    )
    """The agora-bridge server (``tcp://127.0.0.1:7700`` next to the relay, ADR 0045 step 7).
    Unset or empty: no ``agora`` source driver. Set but not reachable (the optional bridge is
    not built or not running): ``agora`` mowers are answered with ``unsupported_provider``, so
    clients in ``auto`` use the direct path."""

    relay_test_hooks: bool = False
    """Serve ``POST /internal/v1/test/freeze`` (pauses the fake source) for the test stacks.
    Refused with ``MOWDECK_ENVIRONMENT=prod``."""

    @model_validator(mode="after")
    def _no_test_hooks_in_production(self) -> Self:
        if self.relay_test_hooks and self.environment == "prod":
            msg = "MOWDECK_RELAY_TEST_HOOKS must not be enabled with MOWDECK_ENVIRONMENT=prod"
            raise ValueError(msg)
        return self

    @property
    def agora_bridge_url(self) -> str | None:
        """The configured bridge server, None when unset or empty."""
        return (self.relay_agora_bridge_url or "").strip() or None

    @property
    def media_enabled(self) -> bool:
        """True when a media server is configured (media plane and source drivers)."""
        return self.relay_mediamtx_api_url is not None

    def to_config(self) -> RelayConfig:
        """The configuration of the use cases."""
        return RelayConfig(whep_base_url=self.relay_whep_base_url)

    def to_policy(self) -> RelayPolicy:
        """Timings and limits of the sessions (ADR 0045 defaults)."""
        return RelayPolicy(max_leases_per_session=self.relay_max_leases_per_session)
