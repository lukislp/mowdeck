"""The use cases of telemetry, wired once at startup."""

from dataclasses import dataclass

from telemetry.application.aerial import Aerial, AerialConfig, TileCache, TileSource
from telemetry.application.context import TelemetryConfig
from telemetry.application.errors import ErrorExplanations
from telemetry.application.health import Health
from telemetry.application.ingest import Ingest
from telemetry.application.ports import Clock, HealthEvents, Store
from telemetry.application.queries import Queries
from telemetry.application.retention import Retention, RetentionObserver
from telemetry.application.tracking import Tracking
from telemetry.application.widget import Widget
from telemetry.application.zone_ports import ZoneStore
from telemetry.application.zones import Zones


@dataclass(frozen=True, slots=True)
class Services:
    """Ingestion, reads, the health history, retention and the aerial photo tiles."""

    ingest: Ingest
    queries: Queries
    health: Health
    retention: Retention
    widget: Widget
    config: TelemetryConfig
    aerial: Aerial
    tracking: Tracking | None = None
    errors: ErrorExplanations | None = None
    """Vendor error codes in plain words (ADR 0067), wired at startup."""
    zones: Zones | None = None
    """Virtual zones (ADR 0070); None without a zone store."""


def build_services(
    store: Store,
    config: TelemetryConfig,
    clock: Clock,
    events: HealthEvents | None = None,
    *,
    aerial_config: AerialConfig | None = None,
    aerial_source: TileSource | None = None,
    aerial_cache: TileCache | None = None,
    retention_observer: RetentionObserver | None = None,
    zone_store: ZoneStore | None = None,
) -> Services:
    """Create the use cases over ``store``; ``events`` publishes the health events and
    ``retention_observer`` receives the numbers of the retention rounds. Without an
    ``aerial_source`` the aerial photo layer is off; without a ``zone_store`` there are no
    virtual zones."""
    queries = Queries(store, config, clock)
    zones = (
        Zones(zone_store, store, clock, max_gap=config.zone_max_gap)
        if zone_store is not None
        else None
    )
    return Services(
        ingest=Ingest(store, config, clock, events, observer=zones),
        queries=queries,
        health=Health(store, config, clock, queries),
        retention=Retention(store, config, clock, observer=retention_observer),
        widget=Widget(store, queries.is_stale, clock),
        config=config,
        aerial=Aerial(
            aerial_config or AerialConfig(),
            queries,
            aerial_source,
            aerial_cache,
            zone_points=zones.zone_points if zones is not None else None,
        ),
        zones=zones,
    )
