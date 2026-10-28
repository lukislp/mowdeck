"""Configuration of the use cases (built from the settings)."""

from dataclasses import dataclass, field
from datetime import timedelta

from telemetry.domain.thinning import ThinPolicy


@dataclass(frozen=True, slots=True)
class TelemetryConfig:
    """Everything the use cases read from the settings."""

    heartbeat_history_seconds: float = 300.0
    """At most one history row per this many seconds for an unchanged state."""
    unknown_mower_attempts: int = 3
    """Deliveries of a command result for an unknown mower before it is dropped."""
    query_max_range: timedelta = timedelta(days=31)
    query_max_rows: int = 5000
    default_range: timedelta = timedelta(days=1)
    states_limit: int = 200
    stale_after: timedelta = timedelta(seconds=60)
    retention_history: timedelta = timedelta(days=30)
    retention_positions: timedelta = timedelta(days=730)
    """Positions older than this are deleted; zero keeps them forever."""
    positions_detail: timedelta = timedelta(days=90)
    """Positions newer than this are never thinned (also the floor for theft tracking)."""
    thinning_enabled: bool = True
    thin_policy: ThinPolicy = field(default_factory=ThinPolicy)
    thinning_zone: str = "UTC"
    """Time zone of the maintenance window."""
    thinning_window_start_hour: int = 1
    thinning_window_end_hour: int = 5
    """Thinning runs while the local hour is in [start, end); equal means always."""
    thinning_budget: timedelta = timedelta(minutes=15)
    """Longest one thinning round runs before it yields the lock."""
    retention_commands: timedelta = timedelta(days=30)
    retention_batch_size: int = 5000
    blade_hours_threshold: float = 50.0
    """Blade hours since the last blade change that trigger ``telemetry.blade_hours.threshold``."""
    health_max_gap: timedelta = timedelta(seconds=120)
    """A longer gap between two states counts no hours and no distance (the mower was silent)."""
    health_max_step_m: float = 25.0
    """A longer step between two positions is skipped in the distance (RTK jumps)."""
    heatmap_max_rows: int = 200_000
    """Most track rows one heatmap reads (newest first; more: ``truncated``)."""
    heatmap_join_seconds: float = 30.0
    """Consecutive blades-on positions at most this far apart in time form one swath."""
    heatmap_join_m: float = 10.0
    """... and at most this far apart in space (both limits must hold)."""
    heatmap_default_range: timedelta = timedelta(days=7)
    """Range of a heatmap without ``from``."""
