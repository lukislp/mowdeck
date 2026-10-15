"""Settings of the telemetry service.

Every field is read from ``MOWDECK_<NAME>`` or from the file named by ``MOWDECK_<NAME>_FILE``.
The field docstrings are the configuration reference (README settings table). OpenFGA is
configured with the ``MOWDECK_OPENFGA_*`` variables of ``mowdeck-authz``, token verification
with the ``MOWDECK_AUTH_*`` variables of ``mowdeck-auth``.
"""

from datetime import timedelta

from mowdeck_service_kit import ServiceSettings
from pydantic import Field

from telemetry.application.context import TelemetryConfig


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

    telemetry_retention_positions_days: int = Field(default=90, ge=1, le=3650)
    """Position track rows older than this are deleted."""

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

    def to_config(self) -> TelemetryConfig:
        """The use case configuration."""
        return TelemetryConfig(
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
