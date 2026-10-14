"""Configuration of the realtime use cases (filled from the settings)."""

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class RealtimeConfig:
    """Limits and timings of the connections."""

    auth_timeout_seconds: float = 5.0
    recheck_seconds: float = 60.0
    max_connections_per_account: int = 50
    max_connections_per_user: int = 10
    max_subscriptions: int = 200
    queue_size: int = 256
    send_timeout_seconds: float = 10.0
    max_message_bytes: int = 16384
    last_state_cache_size: int = 10_000
    close_timeout_seconds: float = 2.0
    """How long a close frame may take before the connection is dropped."""
    drive_max_rate_hz: int = 10
    """Joystick frames per second and session; more are dropped."""
    drive_ttl_ms: int = 1000
    """Dead-man timeout of every frame: the mower stops this long after the last frame."""
    drive_max_seconds: int = 600
    """A drive session ends after this many seconds."""
    drive_request_timeout_seconds: float = 2.0
    """Timeout of the ``fleet.drive.start`` and ``fleet.drive.stop`` requests."""
