"""Prometheus metrics of the delivery worker."""

from prometheus_client import CollectorRegistry, Counter


class DeliveryMetrics:
    """``mowdeck_notifications_deliveries_total{channel, status}``."""

    def __init__(self, registry: CollectorRegistry | None) -> None:
        self._deliveries = Counter(
            "mowdeck_notifications_deliveries",
            "Delivery attempts by channel and result (sent, retry, failed, dropped).",
            ["channel", "status"],
            registry=registry,
        )

    def delivered(self, channel: str, status: str) -> None:
        """Count one finished attempt."""
        self._deliveries.labels(channel=channel, status=status).inc()
