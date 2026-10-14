"""Prometheus metrics of the hub (served by the kit at ``/metrics``)."""

from prometheus_client import CollectorRegistry, Counter, Gauge


class PrometheusMetrics:
    """The :class:`~realtime.application.ports.Metrics` on a Prometheus registry.

    | Metric | Type | Labels |
    | --- | --- | --- |
    | ``mowdeck_realtime_connections`` | gauge | |
    | ``mowdeck_realtime_subscriptions`` | gauge | |
    | ``mowdeck_realtime_messages_sent_total`` | counter | ``type`` |
    | ``mowdeck_realtime_messages_dropped_total`` | counter | ``reason`` |
    | ``mowdeck_realtime_closed_total`` | counter | ``code`` (``none``: the client left) |
    | ``mowdeck_realtime_drive_sessions`` | gauge | |
    | ``mowdeck_realtime_drive_frames_total`` | counter | ``outcome`` (``published``, |
    |  |  | ``rate_limited``, ``failed``) |
    """

    def __init__(self, registry: CollectorRegistry | None) -> None:
        target = registry if registry is not None else CollectorRegistry()
        self._connections = Gauge(
            "mowdeck_realtime_connections", "Open WebSockets of this instance", registry=target
        )
        self._subscriptions = Gauge(
            "mowdeck_realtime_subscriptions",
            "Subscribed (socket, mower) pairs of this instance",
            registry=target,
        )
        self._sent = Counter(
            "mowdeck_realtime_messages_sent",
            "Messages written to sockets",
            ["type"],
            registry=target,
        )
        self._dropped = Counter(
            "mowdeck_realtime_messages_dropped",
            "Messages not delivered (coalesced, slow_consumer, account_mismatch, invalid)",
            ["reason"],
            registry=target,
        )
        self._closed = Counter(
            "mowdeck_realtime_closed",
            "Sockets ended, by close code",
            ["code"],
            registry=target,
        )
        self._drive_sessions = Gauge(
            "mowdeck_realtime_drive_sessions",
            "Open manual drive sessions of this instance",
            registry=target,
        )
        self._drive_frames = Counter(
            "mowdeck_realtime_drive_frames",
            "Joystick frames (published, rate_limited, failed)",
            ["outcome"],
            registry=target,
        )

    def connections(self, count: int) -> None:
        """Open sockets."""
        self._connections.set(count)

    def subscriptions(self, count: int) -> None:
        """Subscribed pairs."""
        self._subscriptions.set(count)

    def sent(self, message_type: str) -> None:
        """One message written."""
        self._sent.labels(type=message_type).inc()

    def dropped(self, reason: str) -> None:
        """One message not delivered."""
        self._dropped.labels(reason=reason).inc()

    def closed(self, code: int | None) -> None:
        """One socket ended."""
        self._closed.labels(code="none" if code is None else str(code)).inc()

    def drive_sessions(self, count: int) -> None:
        """Open drive sessions."""
        self._drive_sessions.set(count)

    def drive_frame(self, outcome: str) -> None:
        """One joystick frame."""
        self._drive_frames.labels(outcome=outcome).inc()
