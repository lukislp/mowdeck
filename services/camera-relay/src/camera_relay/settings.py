"""Settings of the camera-relay service.

Every field is read from ``MOWDECK_<NAME>`` or from the file named by ``MOWDECK_<NAME>_FILE``.
The field docstrings are the configuration reference (README settings table). The timings and
limits of sessions and leases are the defaults of ADR 0045 (``mowdeck_contracts.v1.relay``) and
not configurable.
"""

from mowdeck_service_kit import ServiceSettings
from pydantic import Field, SecretStr

from camera_relay.application.service import RelayConfig
from camera_relay.domain.policy import MAX_LEASES_PER_SESSION, RelayPolicy


class Settings(ServiceSettings):
    """Settings of camera-relay: the kit settings plus service defaults."""

    service_name: str = Field(
        default="camera-relay", min_length=1, max_length=64, pattern=r"^[a-z][a-z0-9\-]*$"
    )
    """Name of the service in logs, metrics and traces."""

    http_port: int = Field(default=8000, ge=1, le=65535)
    """Port the HTTP server listens on."""

    relay_whep_base_url: str = Field(
        default="/whep", min_length=1, max_length=256, pattern=r"^(/|https?://)[^\s]*$"
    )
    """Prefix of the WHEP URLs handed to clients (``<prefix>/r/<relay_id>/<camera>/whep``):
    an absolute path on the MowDeck origin or an ``http(s)`` URL. The route itself comes with
    the media plane."""

    relay_ticket_signing_key: SecretStr | None = None
    """Ed25519 key that signs the tickets: PEM (PKCS#8) or the base64 encoded 32-byte seed.
    Unset: a new key per process (enough for the single replica, which verifies its own
    tickets)."""

    relay_tick_seconds: float = Field(default=1.0, gt=0, le=10)
    """How often deadlines (heartbeats, grace, start timeout, backoff, Vision limits) are
    processed. Event times are exact regardless; this only bounds the delay."""

    relay_max_leases_per_session: int = Field(default=MAX_LEASES_PER_SESSION, ge=1, le=1000)
    """Open leases per account and mower; one more is answered with ``unavailable``."""

    def to_config(self) -> RelayConfig:
        """The configuration of the use cases."""
        return RelayConfig(whep_base_url=self.relay_whep_base_url)

    def to_policy(self) -> RelayPolicy:
        """Timings and limits of the sessions (ADR 0045 defaults)."""
        return RelayPolicy(max_leases_per_session=self.relay_max_leases_per_session)
