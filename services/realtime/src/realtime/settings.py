"""Settings of the realtime service.

Every field is read from ``MOWDECK_<NAME>`` or from the file named by ``MOWDECK_<NAME>_FILE``.
The field docstrings are the configuration reference (README settings table). OpenFGA is
configured with the ``MOWDECK_OPENFGA_*`` variables of ``mowdeck-authz``, token verification
with the ``MOWDECK_AUTH_*`` variables of ``mowdeck-auth``.
"""

from mowdeck_service_kit import ServiceSettings
from pydantic import Field

from realtime.application.config import RealtimeConfig


class Settings(ServiceSettings):
    """Settings of realtime: the kit settings plus service defaults."""

    service_name: str = Field(
        default="realtime", min_length=1, max_length=64, pattern=r"^[a-z][a-z0-9\-]*$"
    )
    """Name of the service in logs, metrics and traces."""

    http_port: int = Field(default=8000, ge=1, le=65535)
    """Port the HTTP server listens on."""

    realtime_auth_timeout_seconds: float = Field(default=5, gt=0, le=60)
    """The first message must be ``auth`` within this time, else the socket closes with 4401."""

    realtime_recheck_seconds: float = Field(default=60, gt=0)
    """Every subscription is checked again this often (besides ``identity.grants.changed`` and
    a token refresh); a mower the principal may no longer view is ``revoked``."""

    realtime_max_connections_per_account: int = Field(default=50, ge=1, le=100_000)
    """Open sockets per account and instance; one more is answered with 4429."""

    realtime_max_connections_per_user: int = Field(default=10, ge=1, le=10_000)
    """Open sockets per user (or API token) and instance; one more is answered with 4429."""

    realtime_max_subscriptions: int = Field(default=200, ge=1, le=1000)
    """Subscribed mowers per socket; a ``subscribe`` beyond it is answered with 4429."""

    realtime_queue_size: int = Field(default=256, ge=1, le=100_000)
    """Unsent messages other than ``state`` per socket; an overflow closes it with 4430."""

    realtime_send_timeout_seconds: float = Field(default=10, gt=0)
    """A single send that takes longer closes the socket with 4430 (slow consumer)."""

    realtime_ping_seconds: float = Field(default=20, ge=0)
    """Interval of the WebSocket protocol pings sent by uvicorn; a pong must arrive within the
    same time or uvicorn closes the socket. 0 disables them."""

    realtime_max_message_bytes: int = Field(default=16384, ge=1024, le=1_048_576)
    """Largest client frame; a larger one closes the socket with 1009."""

    realtime_last_state_cache_size: int = Field(default=10_000, ge=0, le=1_000_000)
    """Mowers whose newest state is kept, so a new subscription gets the current state at
    once. 0 disables the cache."""

    realtime_drive_max_seconds: int = Field(default=600, ge=1, le=3600)
    """A manual drive session (joystick) ends after this many seconds; the client may start a
    new one."""

    realtime_drive_max_rate_hz: int = Field(default=10, ge=1, le=50)
    """Joystick frames per second and session that are forwarded to the mower; more are
    dropped (``mowdeck_realtime_drive_frames_total{outcome="rate_limited"}``)."""

    realtime_drive_ttl_ms: int = Field(default=1000, ge=100, le=2000)
    """Dead-man timeout of every frame (``ManualDrive.ttl_ms``): the mower stops this long
    after the last frame at the latest."""

    realtime_drive_request_timeout_seconds: float = Field(default=2, gt=0, le=30)
    """Timeout of the ``fleet.drive.start`` and ``fleet.drive.stop`` requests to Fleet."""

    realtime_allowed_origins: str = ""
    """Comma separated ``Origin`` values allowed to open a socket; empty allows every origin
    (authentication is a token in a message, never a cookie)."""

    def to_config(self) -> RealtimeConfig:
        """The configuration of the use cases."""
        return RealtimeConfig(
            auth_timeout_seconds=self.realtime_auth_timeout_seconds,
            recheck_seconds=self.realtime_recheck_seconds,
            max_connections_per_account=self.realtime_max_connections_per_account,
            max_connections_per_user=self.realtime_max_connections_per_user,
            max_subscriptions=self.realtime_max_subscriptions,
            queue_size=self.realtime_queue_size,
            send_timeout_seconds=self.realtime_send_timeout_seconds,
            max_message_bytes=self.realtime_max_message_bytes,
            last_state_cache_size=self.realtime_last_state_cache_size,
            drive_max_rate_hz=self.realtime_drive_max_rate_hz,
            drive_ttl_ms=self.realtime_drive_ttl_ms,
            drive_max_seconds=self.realtime_drive_max_seconds,
            drive_request_timeout_seconds=self.realtime_drive_request_timeout_seconds,
        )

    @property
    def allowed_origins(self) -> frozenset[str]:
        """The parsed ``realtime_allowed_origins``."""
        return frozenset(o.strip() for o in self.realtime_allowed_origins.split(",") if o.strip())
