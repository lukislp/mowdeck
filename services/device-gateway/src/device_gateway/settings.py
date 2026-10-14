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
    including a frame of the capture worker (stream token, the mower starting to publish, the
    first decoded frame). Keep it below Vision's ``VISION_PHOTO_TIMEOUT_SECONDS``."""

    capture_url: str | None = None
    """Base URL of the capture worker (``http://vision-capture:8000``, ADR 0035). When set,
    mowers whose adapter offers a live camera but cannot take photos (the Mammotion cloud
    connection) answer photo requests with a frame of their camera. Empty: no such photos."""

    capture_token: SecretStr | None = None
    """Shared secret sent to the capture worker (``MOWDECK_CAPTURE_TOKEN`` there)."""

    capture_timeout_seconds: float = Field(default=20.0, gt=0, le=60)
    """How long the capture worker may wait for the first decoded frame."""

    capture_max_width: int = Field(default=1280, ge=160, le=3840)
    """Frames wider than this are scaled down by the worker."""

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
    """Directory for the PyMammotion credential caches of the shared cloud sessions, one file
    per account (live tokens: a private, writable volume). Unset: no cache, a password login
    on every new session."""

    fake_mammotion_cloud: bool = False
    """**Development only.** Answer discoveries and connect ``cloud`` mowers through the fake
    Mammotion cloud (accounts ``*@fake.mowdeck.test``, password ``fake-cloud-password``)
    instead of the real one; refused with ``MOWDECK_ENVIRONMENT=prod``."""

    @model_validator(mode="after")
    def _shard_in_range(self) -> Self:
        if self.shard_index >= self.shard_count:
            msg = "MOWDECK_SHARD_INDEX must be smaller than MOWDECK_SHARD_COUNT"
            raise ValueError(msg)
        if self.fake_mammotion_cloud and self.environment == "prod":
            msg = "MOWDECK_FAKE_MAMMOTION_CLOUD is for development only (not with environment prod)"
            raise ValueError(msg)
        return self

    @property
    def command_consumer(self) -> str:
        """Durable JetStream consumer of this shard for ``command.mower.*``."""
        return f"device-gateway-{self.shard_index}"
