"""Settings of the telemetry service.

Every field is read from ``MOWDECK_<NAME>`` or from the file named by ``MOWDECK_<NAME>_FILE``.
The field docstrings are the configuration reference (README settings table). OpenFGA is
configured with the ``MOWDECK_OPENFGA_*`` variables of ``mowdeck-authz``, token verification
with the ``MOWDECK_AUTH_*`` variables of ``mowdeck-auth``.
"""

from datetime import timedelta
from typing import Self
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from mowdeck_service_kit import ServiceSettings
from pydantic import Field, field_validator, model_validator

from telemetry.application.aerial import AerialConfig
from telemetry.application.context import TelemetryConfig
from telemetry.domain.thinning import ThinPolicy


class Settings(ServiceSettings):
    """Settings of telemetry: the kit settings plus service defaults."""

    service_name: str = Field(
        default="telemetry", min_length=1, max_length=64, pattern=r"^[a-z][a-z0-9\-]*$"
    )
    """Name of the service in logs, metrics and traces."""

    http_port: int = Field(default=8000, ge=1, le=65535)
    """Port the HTTP server listens on."""

    telemetry_heartbeat_history_seconds: float = Field(default=300, ge=1)
    """At most one history row per this many seconds for an unchanged state (the gateway
    republishes an unchanged state every 10 s as a heartbeat); a change is always stored."""

    telemetry_retention_history_days: int = Field(default=30, ge=1, le=3650)
    """History rows older than this are deleted."""

    telemetry_retention_positions_days: int = Field(default=730, ge=0, le=36500)
    """Position track rows older than this are deleted; 0 keeps them forever (ADR 0053).
    Rows older than ``MOWDECK_TELEMETRY_POSITIONS_DETAIL_DAYS`` are thinned first."""

    telemetry_positions_detail_days: int = Field(default=90, ge=7, le=3650)
    """Positions newer than this keep every row; older ones are downsampled (the floor for
    anything that reads the raw track, such as theft tracking, ADR 0053)."""

    telemetry_positions_thinning_enabled: bool = True
    """Downsample old positions in the nightly window. Off: old rows are only deleted."""

    telemetry_positions_thin_active_seconds: int = Field(default=30, ge=1, le=300)
    """Old positions: one row per this many seconds while the blades run or the mower moves
    (must divide 300)."""

    telemetry_positions_thin_idle_seconds: int = Field(default=300, ge=1, le=300)
    """Old positions: one row per this many seconds while the mower stands (must divide 300)."""

    telemetry_maintenance_timezone: str = "UTC"
    """IANA time zone of the maintenance window (for example ``Europe/Berlin``)."""

    telemetry_thinning_window_start_hour: int = Field(default=1, ge=0, le=23)
    """Local hour at which thinning may start."""

    telemetry_thinning_window_end_hour: int = Field(default=5, ge=0, le=23)
    """Local hour at which thinning must stop (exclusive); equal to the start: always."""

    telemetry_thinning_budget_seconds: float = Field(default=900, ge=1)
    """Longest one thinning round runs before it releases the lock until the next round."""

    telemetry_retention_commands_days: int = Field(default=30, ge=1, le=3650)
    """Command timeline rows older than this are deleted."""

    telemetry_retention_interval_seconds: float = Field(default=3600, ge=10)
    """How often the retention job runs (one replica at a time, advisory lock)."""

    telemetry_query_max_range_days: int = Field(default=31, ge=1, le=3650)
    """Longest time range of a history or position query (422 above)."""

    telemetry_query_max_rows: int = Field(default=5000, ge=10, le=100_000)
    """Most points a history or position query returns (more: ``truncated``; a ``step`` that
    needs more bins is refused)."""

    telemetry_states_limit: int = Field(default=200, ge=1, le=10_000)
    """Most mowers in ``GET /accounts/me/mowers/states``."""

    telemetry_stale_after_seconds: float = Field(default=60, ge=1)
    """A mower whose last link heartbeat is older than this is reported with ``stale: true``
    (six missed heartbeats, ADR 0040)."""

    telemetry_unknown_mower_attempts: int = Field(default=3, ge=1, le=20)
    """Deliveries of a command result for a mower missing from the directory before it is
    dropped with a warning (Fleet's event may arrive a moment later)."""

    telemetry_fleet_timeout_seconds: float = Field(default=5, gt=0)
    """Timeout of the ``fleet.mowers.list`` request that seeds the mower directory."""

    telemetry_fleet_retry_max_seconds: float = Field(default=30, ge=1)
    """Upper bound of the backoff between seeding attempts (Fleet may start later)."""

    telemetry_blade_hours_threshold: float = Field(default=50.0, gt=0)
    """Blade hours since the last recorded blade change after which Telemetry publishes
    ``telemetry.blade_hours.threshold`` once (again after the next blade reset)."""

    telemetry_health_max_gap_seconds: float = Field(default=120, gt=0)
    """A gap between two states longer than this counts no blade or mowing hours and no
    distance for the health history (the mower was silent; the gateway heartbeat is 10 s)."""

    telemetry_health_max_step_meters: float = Field(default=25, gt=0)
    """A step between two consecutive positions longer than this is left out of the distance
    (filters RTK jumps)."""

    telemetry_heatmap_max_rows: int = Field(default=200_000, ge=1000, le=2_000_000)
    """Most position rows one heatmap reads, newest first (more: ``truncated``)."""

    telemetry_heatmap_join_seconds: float = Field(default=30, gt=0, le=3600)
    """Coverage heatmap: consecutive blades-on positions at most this many seconds apart are
    joined by a straight swath (the cloud reports a mowing LUBA every 1 to 20 s)."""

    telemetry_heatmap_join_metres: float = Field(default=10, gt=0, le=1000)
    """Coverage heatmap: ... and at most this many metres apart (both limits must hold; about
    0.6 m/s for 20 s with margin). Farther positions stay separate discs (RTK jumps)."""

    telemetry_aerial_wms_url: str | None = Field(default=None, pattern=r"^https?://[^\s\"<>\\]+$")
    """Aerial photo under the map: base URL of a WMS (https) that can answer ``GetMap`` in
    EPSG:3857. Empty (the default) means no aerial layer and no request to any third party.
    Bavaria: ``https://geoservices.bayern.de/od/wms/dop/v1/dop40`` (ADR 0054)."""

    telemetry_aerial_allow_http: bool = False
    """Accept ``http://`` source URLs. Only for the fake source of the test stack: real sources
    are https, and the default refuses a plain http URL."""

    telemetry_aerial_wms_layer: str = Field(default="", max_length=200, pattern=r"^[^\s&?=#]*$")
    """Layer name of the WMS (Bavaria: ``by_dop40c``, colour)."""

    telemetry_aerial_wms_version: str = Field(default="1.3.0", pattern=r"^1\.[13]\.\d$")
    """WMS version (``1.3.0`` or ``1.1.1``)."""

    telemetry_aerial_image_format: str = Field(default="image/jpeg", pattern=r"^image/(jpeg|png)$")
    """Image format requested from the WMS."""

    telemetry_aerial_xyz_url: str | None = Field(
        default=None,
        pattern=r"^https?://[^\s\"<>\\]*\{z\}[^\s\"<>\\]*\{x\}[^\s\"<>\\]*\{y\}[^\s\"<>\\]*$",
    )
    """Alternative to the WMS: an https XYZ tile URL template with ``{z}``, ``{x}``, ``{y}`` (Web
    Mercator, 256 px). Takes precedence over the WMS."""

    telemetry_aerial_attribution: str = Field(default="", max_length=300)
    """Credit line the client shows on the map while the photo is on (the licence usually
    requires it)."""

    telemetry_aerial_min_zoom: int = Field(default=14, ge=0, le=24)
    """Lowest zoom served."""

    telemetry_aerial_max_zoom: int = Field(default=19, ge=0, le=24)
    """Highest zoom the source has real detail for (the client scales beyond it)."""

    telemetry_aerial_tile_size: int = Field(default=256, ge=128, le=1024)
    """Pixel size of the WMS request for one tile."""

    telemetry_aerial_margin_meters: float = Field(default=60, ge=0, le=2000)
    """Tiles are served this far around the outline of the map (areas, zones, paths, dock)."""

    telemetry_aerial_cache_dir: str = Field(default="/var/cache/mowdeck/aerial", min_length=1)
    """Directory of the tile cache (a volume; unwritable means no caching)."""

    telemetry_aerial_cache_max_mb: int = Field(default=256, ge=1, le=100_000)
    """Size limit of the tile cache; the oldest tiles are deleted above it."""

    telemetry_aerial_cache_ttl_days: int = Field(default=30, ge=1, le=3650)
    """A cached tile is fetched again after this many days (new flights, 40 cm DOP: yearly)."""

    telemetry_aerial_misses_per_minute: int = Field(default=240, ge=1, le=100_000)
    """New tiles (not in the cache) one account may fetch from the source per minute."""

    telemetry_aerial_timeout_seconds: float = Field(default=10, gt=0, le=120)
    """Timeout of a request to the photo source."""

    @field_validator("telemetry_aerial_wms_url", "telemetry_aerial_xyz_url", mode="before")
    @classmethod
    def _empty_url_is_none(cls, value: object) -> object:
        """Compose passes unset variables as empty strings."""
        return None if isinstance(value, str) and not value.strip() else value

    @model_validator(mode="after")
    def _https_sources(self) -> Self:
        for url in (self.telemetry_aerial_wms_url, self.telemetry_aerial_xyz_url):
            if url and not url.startswith("https://") and not self.telemetry_aerial_allow_http:
                msg = "the aerial photo source must be an https URL"
                raise ValueError(msg)
        return self

    def to_aerial_config(self) -> AerialConfig:
        """The aerial photo configuration."""
        return AerialConfig(
            wms_url=self.telemetry_aerial_wms_url or None,
            wms_layer=self.telemetry_aerial_wms_layer,
            wms_version=self.telemetry_aerial_wms_version,
            image_format=self.telemetry_aerial_image_format,
            xyz_url=self.telemetry_aerial_xyz_url or None,
            attribution=self.telemetry_aerial_attribution,
            min_zoom=self.telemetry_aerial_min_zoom,
            max_zoom=self.telemetry_aerial_max_zoom,
            tile_size=self.telemetry_aerial_tile_size,
            margin_m=self.telemetry_aerial_margin_meters,
            cache_ttl=timedelta(days=self.telemetry_aerial_cache_ttl_days),
            misses_per_minute=self.telemetry_aerial_misses_per_minute,
        )

    @model_validator(mode="after")
    def _check_positions_policy(self) -> "Settings":
        days = self.telemetry_retention_positions_days
        if 0 < days < self.telemetry_positions_detail_days:
            msg = "MOWDECK_TELEMETRY_RETENTION_POSITIONS_DAYS must be 0 or at least the detail days"
            raise ValueError(msg)
        try:
            ZoneInfo(self.telemetry_maintenance_timezone)
        except (ZoneInfoNotFoundError, ValueError) as error:
            msg = f"unknown time zone {self.telemetry_maintenance_timezone!r}"
            raise ValueError(msg) from error
        ThinPolicy(
            active_seconds=self.telemetry_positions_thin_active_seconds,
            idle_seconds=self.telemetry_positions_thin_idle_seconds,
        )
        return self

    def to_config(self) -> TelemetryConfig:
        """The use case configuration."""
        return TelemetryConfig(
            positions_detail=timedelta(days=self.telemetry_positions_detail_days),
            thinning_enabled=self.telemetry_positions_thinning_enabled,
            thin_policy=ThinPolicy(
                active_seconds=self.telemetry_positions_thin_active_seconds,
                idle_seconds=self.telemetry_positions_thin_idle_seconds,
            ),
            thinning_zone=self.telemetry_maintenance_timezone,
            thinning_window_start_hour=self.telemetry_thinning_window_start_hour,
            thinning_window_end_hour=self.telemetry_thinning_window_end_hour,
            thinning_budget=timedelta(seconds=self.telemetry_thinning_budget_seconds),
            heartbeat_history_seconds=self.telemetry_heartbeat_history_seconds,
            unknown_mower_attempts=self.telemetry_unknown_mower_attempts,
            query_max_range=timedelta(days=self.telemetry_query_max_range_days),
            query_max_rows=self.telemetry_query_max_rows,
            states_limit=self.telemetry_states_limit,
            stale_after=timedelta(seconds=self.telemetry_stale_after_seconds),
            retention_history=timedelta(days=self.telemetry_retention_history_days),
            retention_positions=timedelta(days=self.telemetry_retention_positions_days),
            retention_commands=timedelta(days=self.telemetry_retention_commands_days),
            blade_hours_threshold=self.telemetry_blade_hours_threshold,
            health_max_gap=timedelta(seconds=self.telemetry_health_max_gap_seconds),
            health_max_step_m=self.telemetry_health_max_step_meters,
            heatmap_max_rows=self.telemetry_heatmap_max_rows,
            heatmap_join_seconds=self.telemetry_heatmap_join_seconds,
            heatmap_join_m=self.telemetry_heatmap_join_metres,
        )
