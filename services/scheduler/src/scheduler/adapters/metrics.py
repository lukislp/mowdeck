"""Prometheus metrics of the runner."""

from prometheus_client import CollectorRegistry, Counter


class RunMetrics:
    """``mowdeck_scheduler_runs_total{trigger, outcome}``."""

    def __init__(self, registry: CollectorRegistry | None) -> None:
        self._runs = Counter(
            "mowdeck_scheduler_runs",
            "Schedule runs by trigger (schedule, manual) and outcome.",
            ["trigger", "outcome"],
            registry=registry,
        )

    def recorded(self, trigger: str, outcome: str) -> None:
        """Count one run record."""
        self._runs.labels(trigger=trigger, outcome=outcome).inc()
