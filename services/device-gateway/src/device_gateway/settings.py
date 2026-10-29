"""Settings of the device-gateway service.

Every field is read from ``MOWDECK_<NAME>`` or from the file named by ``MOWDECK_<NAME>_FILE``.
The field docstrings are the configuration reference (README settings table).
"""

from pathlib import Path
from typing import Self

from mowdeck_contracts.v1 import MowerRegistration
from mowdeck_service_kit import ServiceSettings
from pydantic import Field, SecretStr, model_validator


class Settings(ServiceSettings):
    """Settings of device-gateway: the kit settings plus service defaults."""

    service_name: str = Field(
        default="device-gateway", min_length=1, max_length=64, pattern=r"^[a-z][a-z0-9\-]*$"
    )
    """Name of the service in logs, metrics and traces."""

    http_port: int = Field(default=8000, ge=1, le=65535)
    """Port the HTTP server listens on."""

    shard_index: int = Field(default=0, ge=0)
    """Shard of this instance: it holds the mowers with ``mower_id.int % shard_count == index``."""

    shard_count: int = Field(default=1, ge=1, le=1024)
    """Number of Device-Gateway instances (shards)."""

    bootstrap_mowers: list[MowerRegistration] = Field(default_factory=list)
    """Development only: JSON list of ``MowerRegistration`` seeding the registry without Fleet."""

    telemetry_heartbeat_seconds: float = Field(default=10.0, gt=0, le=3600)
    snapshot_recheck_seconds: float = Field(
        default=30.0,
        gt=0,
        le=3600,
        description="How often capabilities and map are compared again while states arrive; "
        "only changes are republished (model, limits and dock arrive after connecting).",
    )
    """Publish the latest state at least this often, even when nothing changed."""

    telemetry_drive_heartbeat_seconds: float = Field(default=1.0, gt=0, le=60)
    """The same while a manual or automated drive session is open (AI assist watches it)."""

    sim_time_scale: float = Field(default=1.0, gt=0, le=10_000)
    """Simulated seconds per real second for simulated mowers (unless the registration sets
    ``time_scale`` in its connection options)."""

    sim_tick_seconds: float = Field(default=1.0, gt=0, le=60)
    """Real seconds between two simulation steps of a simulated mower."""

    fleet_request_timeout_seconds: float = Field(default=2.0, gt=0, le=60)
    """Timeout of the ``fleet.mowers.list`` request."""

    photo_timeout_seconds: float = Field(default=30.0, gt=0, le=120)
    """Longest time an adapter may take for a still image (``gateway.mower.<id>.photo``),
    including a frame of the camera relay (the mower starting to publish, the first decoded
    frame). Keep it below Vision's ``VISION_PHOTO_TIMEOUT_SECONDS``."""

    path_preview_timeout_seconds: float = Field(default=75.0, gt=0, le=120)
    """Longest wait for a route the mower plans for a preview (``gateway.mower.<id>.path.preview``):
    the mower plans, then PyMammotion fetches the line list and the cover path frames. Keep it
    below Fleet's ``MOWDECK_PATH_PREVIEW_REQUEST_TIMEOUT_SECONDS``, so Fleet gets the ``timeout``
    reply."""

    path_progress_seconds: float = Field(default=5.0, gt=0, le=600)
    """A mow path snapshot whose only change is the progress is republished at most this often."""

    firmware_check_initial_seconds: float = Field(default=120.0, ge=0, le=86_400)
    """Wait after a connect before the manufacturer is asked for firmware updates (after the
    adapter's device information rounds), for connections with ``capabilities.firmware_update``
    (ADR 0038). 0 switches the periodic check off; ``gateway.mower.<id>.firmware.check`` still
    works."""

    firmware_check_interval_seconds: float = Field(default=24 * 3600.0, ge=60, le=30 * 86_400)
    """Then ask again this often while the connection lasts."""

    relay_url: str | None = None
    """Base URL of the camera relay (``http://camera-relay:8000``, ADR 0045). With
    ``relay_frames_token`` set, mowers whose adapter offers a live camera but cannot take photos
    (the Mammotion cloud connection) answer photo requests with a frame from the relay: it
    reuses the session a viewer holds or starts one within the Vision limits. Empty: no relay
    frames."""

    relay_frames_token: SecretStr | None = None
    """Shared secret of the relay's frame tap (``MOWDECK_RELAY_FRAMES_TOKEN`` there). Empty:
    no relay frames."""

    capture_timeout_seconds: float = Field(default=20.0, gt=0, le=60)
    """How long the camera relay may take for one frame (it holds a snapshot lease at most 30 s).
    The name stays from the removed capture worker (ADR 0035)."""

    capture_max_width: int = Field(default=1280, ge=160, le=3840)
    """Frames wider than this are scaled down by the relay."""

    fleet_retry_max_seconds: float = Field(default=30.0, gt=0, le=3600)
    """Upper bound of the backoff between ``fleet.mowers.list`` attempts."""

    reconnect_initial_seconds: float = Field(default=1.0, gt=0, le=60)
    """First delay before reconnecting a dropped or failed mower connection."""

    reconnect_max_seconds: float = Field(default=30.0, gt=0, le=3600)
    """Upper bound of the reconnect backoff."""

    connection_check_seconds: float = Field(default=2.0, gt=0, le=60)
    """How often an established connection is checked for a dropped link."""

    cloud_discovery_timeout_seconds: float = Field(default=40.0, gt=0, le=300)
    """Longest time a ``gateway.cloud.discover`` login and device listing may take (Fleet's
    own timeout should be a little longer)."""

    cloud_discovery_concurrency: int = Field(default=2, ge=1, le=16)
    """Vendor logins of discoveries that may run at once on this instance (more wait)."""

    cloud_session_cache_dir: Path | None = None
    """Directory for the session caches of the cloud providers (credential caches of the shared
    cloud sessions, live tokens: a private, writable volume). Unset: no cache, a login on every
    new session."""

    cloud_providers_enable: str = ""
    """Comma separated cloud provider ids to enable in addition to the providers that are on by
    default (those tested on real hardware), e.g. ``husqvarna``. Providers that were never tested
    on real hardware are off until listed here (ADR 0065)."""

    cloud_providers_disable: str = ""
    """Comma separated cloud provider ids to switch off (wins over the enable list and the
    defaults)."""

    fake_clouds: bool = False
    """**Development only.** Every enabled cloud provider answers discoveries and connects its
    ``cloud`` mowers through the fake cloud of its adapter package (fixed test accounts, see the
    package READMEs) instead of the vendor's; refused with ``MOWDECK_ENVIRONMENT=prod``."""

    fake_mammotion_cloud: bool = False
    """Deprecated name of ``fake_clouds`` (``MOWDECK_FAKE_MAMMOTION_CLOUD``), still honoured."""

    @property
    def use_fake_clouds(self) -> bool:
        """``fake_clouds`` or its deprecated name."""
        return self.fake_clouds or self.fake_mammotion_cloud

    @property
    def enabled_cloud_providers(self) -> tuple[str, ...]:
        """``cloud_providers_enable`` as ids."""
        return _ids(self.cloud_providers_enable)

    @property
    def disabled_cloud_providers(self) -> tuple[str, ...]:
        """``cloud_providers_disable`` as ids."""
        return _ids(self.cloud_providers_disable)

    @model_validator(mode="after")
    def _shard_in_range(self) -> Self:
        if self.shard_index >= self.shard_count:
            msg = "MOWDECK_SHARD_INDEX must be smaller than MOWDECK_SHARD_COUNT"
            raise ValueError(msg)
        if self.use_fake_clouds and self.environment == "prod":
            msg = "MOWDECK_FAKE_CLOUDS is for development only (not with environment prod)"
            raise ValueError(msg)
        return self

    @property
    def command_consumer(self) -> str:
        """Durable JetStream consumer of this shard for ``command.mower.*``."""
        return f"device-gateway-{self.shard_index}"


def _ids(value: str) -> tuple[str, ...]:
    return tuple(sorted({p.strip().lower() for p in value.split(",") if p.strip()}))
