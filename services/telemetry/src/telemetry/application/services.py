"""The use cases of telemetry, wired once at startup."""

from dataclasses import dataclass

from telemetry.application.context import TelemetryConfig
from telemetry.application.health import Health
from telemetry.application.ingest import Ingest
from telemetry.application.ports import Clock, HealthEvents, Store
from telemetry.application.queries import Queries
from telemetry.application.retention import Retention, RetentionObserver


@dataclass(frozen=True, slots=True)
class Services:
    """Ingestion, reads, the health history and retention."""

    ingest: Ingest
    queries: Queries
    health: Health
    retention: Retention
    config: TelemetryConfig


def build_services(
    store: Store,
    config: TelemetryConfig,
    clock: Clock,
    events: HealthEvents | None = None,
    retention_observer: RetentionObserver | None = None,
) -> Services:
    """Create the use cases over ``store``; ``events`` publishes the health events and
    ``retention_observer`` receives the numbers of the retention rounds."""
    queries = Queries(store, config, clock)
    return Services(
        ingest=Ingest(store, config, clock, events),
        queries=queries,
        health=Health(store, config, clock, queries),
        retention=Retention(store, config, clock, observer=retention_observer),
        config=config,
    )
