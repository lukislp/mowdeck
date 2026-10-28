"""Settings of the telemetry service.

Every field is read from ``MOWDECK_<NAME>`` or from the file named by ``MOWDECK_<NAME>_FILE``.
The field docstrings are the configuration reference (README settings table). OpenFGA is
configured with the ``MOWDECK_OPENFGA_*`` variables of ``mowdeck-authz``, token verification
with the ``MOWDECK_AUTH_*`` variables of ``mowdeck-auth``.
"""

from datetime import timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from mowdeck_service_kit import ServiceSettings
from pydantic import Field, model_validator

from telemetry.application.context import TelemetryConfig
from telemetry.application.tracking import TrackingConfig
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
    """Position track rows older than this are deleted; 0 keeps them forever (ADR 0058).
    Rows older than ``MOWDECK_TELEMETRY_POSITIONS_DETAIL_DAYS`` are thinned first."""

    telemetry_positions_detail_days: int = Field(default=90, ge=7, le=3650)
    """Positions newer than this keep every row; older ones are downsampled (the floor for
    anything that reads the raw track, such as theft tracking, ADR 0058)."""

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

    telemetry_tracking_interval_seconds: float = Field(default=10.0, ge=5, le=3600)
    """Theft tracking (ADR 0058): position report interval asked from the gateway while a
    session runs (the adapter never goes below 5 s)."""

    telemetry_tracking_max_hours: float = Field(default=24.0, gt=0, le=720)
    """A tracking session ends by itself after this many hours (with a notification)."""

    telemetry_tracking_tick_seconds: float = Field(default=15.0, ge=1, le=300)
    """How often active sessions are looked at (lease renewal, stale and move checks)."""

    telemetry_tracking_lease_seconds: float = Field(default=60.0, ge=5, le=3600)
    """Length of the gateway lease; the gateway returns to its normal rhythm this long after the
    last renewal. Keep it a few ticks long."""

    telemetry_tracking_stale_after_seconds: float = Field(default=120.0, ge=10)
    """No report for this long marks the session stale and notifies once per outage."""

    telemetry_tracking_move_meters: float = Field(default=50.0, gt=0)
    """A "moved" notification needs this distance from the previous notice ..."""

    telemetry_tracking_move_notice_seconds: float = Field(default=300.0, ge=0)
    """... and this long since it."""

    telemetry_tracking_link_ttl_hours: float = Field(default=48.0, gt=0, le=720)
    """Lifetime of a share link (and the longest one may ask for)."""

    telemetry_tracking_max_links: int = Field(default=10, ge=1, le=100)
    """Active share links per session."""

    telemetry_tracking_retention_days: int = Field(default=30, ge=1, le=3650)
    """Ended sessions with their links and photos are deleted after this many days."""

    telemetry_tracking_auto_start: bool = True
    """Start a session on a theft alarm (lifted, left the dock, geofence exit)."""

    telemetry_tracking_photos: bool = True
    """Take a camera photo at the start for the report."""

    telemetry_tracking_public_rate_per_minute: int = Field(default=60, ge=1)
    """Requests per minute and client to the public share page."""

    telemetry_tracking_public_failures_per_minute: int = Field(default=20, ge=1)
    """Unusable tokens per minute and client before the client is slowed down."""

    def to_tracking_config(self) -> TrackingConfig:
        """The tracking use case configuration."""
        return TrackingConfig(
            interval_seconds=self.telemetry_tracking_interval_seconds,
            max_duration=timedelta(hours=self.telemetry_tracking_max_hours),
            tick_seconds=self.telemetry_tracking_tick_seconds,
            lease_seconds=self.telemetry_tracking_lease_seconds,
            stale_after=timedelta(seconds=self.telemetry_tracking_stale_after_seconds),
            move_meters=self.telemetry_tracking_move_meters,
            move_notice_interval=timedelta(seconds=self.telemetry_tracking_move_notice_seconds),
            link_ttl=timedelta(hours=self.telemetry_tracking_link_ttl_hours),
            max_links=self.telemetry_tracking_max_links,
            retention=timedelta(days=self.telemetry_tracking_retention_days),
            auto_start=self.telemetry_tracking_auto_start,
            photos=self.telemetry_tracking_photos,
            public_rate_per_minute=self.telemetry_tracking_public_rate_per_minute,
            public_failures_per_minute=self.telemetry_tracking_public_failures_per_minute,
        )

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
