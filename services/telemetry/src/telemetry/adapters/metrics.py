"""Prometheus metrics of the retention rounds (ADR 0053)."""

from prometheus_client import CollectorRegistry, Counter, Histogram


class PrometheusRetentionObserver:
    """Implements :class:`~telemetry.application.retention.RetentionObserver`."""

    def __init__(self, registry: CollectorRegistry) -> None:
        self._deleted = Counter(
            "mowdeck_telemetry_retention_rows_deleted_total",
            "Rows deleted by the age limit, by table.",
            ["table"],
            registry=registry,
        )
        self._scanned = Counter(
            "mowdeck_telemetry_position_thinning_rows_scanned_total",
            "Position rows examined by the downsampling.",
            registry=registry,
        )
        self._thinned = Counter(
            "mowdeck_telemetry_position_thinning_rows_deleted_total",
            "Position rows removed by the downsampling.",
            registry=registry,
        )
        self._seconds = Histogram(
            "mowdeck_telemetry_retention_round_seconds",
            "Duration of a retention round (delete or thin).",
            ["kind"],
            buckets=(0.1, 0.5, 1, 5, 15, 60, 300, 900, 1800),
            registry=registry,
        )

    def rows_deleted(self, table: str, count: int) -> None:
        """Rows removed by the age limit."""
        self._deleted.labels(table).inc(count)

    def rows_thinned(self, scanned: int, deleted: int) -> None:
        """Positions looked at and removed by the downsampling."""
        self._scanned.inc(scanned)
        self._thinned.inc(deleted)

    def round_finished(self, kind: str, seconds: float) -> None:
        """A round ended after this many seconds."""
        self._seconds.labels(kind).observe(seconds)
