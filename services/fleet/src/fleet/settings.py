"""Settings of the fleet service.

Every field is read from ``MOWDECK_<NAME>`` or from the file named by ``MOWDECK_<NAME>_FILE``.
The field docstrings are the configuration reference (README settings table). OpenFGA is
configured with the ``MOWDECK_OPENFGA_*`` variables of ``mowdeck-authz``, token verification
with the ``MOWDECK_AUTH_*`` variables of ``mowdeck-auth``.
"""

from datetime import timedelta
from typing import Self

from mowdeck_service_kit import ServiceSettings
from pydantic import Field, SecretStr, model_validator

from fleet.application.context import FleetConfig
from fleet.domain.rules import CommandLimits


class Settings(ServiceSettings):
    """Settings of fleet: the kit settings plus service defaults."""

    service_name: str = Field(
        default="fleet", min_length=1, max_length=64, pattern=r"^[a-z][a-z0-9\-]*$"
    )
    """Name of the service in logs, metrics and traces."""

    http_port: int = Field(default=8000, ge=1, le=65535)
    """Port the HTTP server listens on."""

    command_ttl_seconds: int = Field(default=30, ge=1, le=3600)
    """Default expiry of a command: the gateway drops it when it arrives later."""

    command_ttl_max_seconds: int = Field(default=300, ge=1, le=3600)
    """Upper bound for ``ttl_seconds`` in a command request."""

    emergency_stop_ttl_seconds: int = Field(default=10, ge=1, le=60)
    """Expiry of an emergency stop (short: a late stop must not surprise anybody)."""

    manual_drive_ttl_seconds: int = Field(default=2, ge=1, le=10)
    """Expiry of a manual drive frame (the dead-man ``ttl_ms`` lives in the command)."""

    command_retention_days: int = Field(default=90, ge=1, le=3650)
    """Command records (the command audit) older than this are deleted."""

    command_retention_interval_seconds: float = Field(default=3600, ge=10)
    """How often the retention job runs (one replica at a time, advisory lock)."""

    max_mowers_per_account: int = Field(default=50, ge=1, le=100_000)
    """Mowers an account may register (until Entitlements decides per plan)."""

    allowed_connection_kinds: str = "simulated"
    """Comma separated connection kinds accepted at registration (``simulated``, ``cloud``,
    ``ble_bridge``; the real kinds need ``mowdeck-control-mammotion`` in the Device-Gateway)."""

    connection_options_max_bytes: int = Field(default=4096, ge=2, le=65536)
    """Maximum size of a mower's ``connection_options`` as JSON."""

    bulk_max_mowers: int = Field(default=200, ge=1, le=10_000)
    """Maximum number of mowers a bulk command may address (422 above)."""

    default_timezone: str = "Europe/Berlin"
    """Time zone of the hidden default site of new accounts (IANA name)."""

    spot_plan_max_bytes: int = Field(default=262_144, ge=1024, le=900_000)
    """Largest spot plan a ``start_mow`` may carry, as JSON (422 above; it has to fit one bus
    message together with the envelope)."""

    camera_request_timeout_seconds: float = Field(default=15.0, ge=1, le=60)
    """How long Fleet waits for the Device-Gateway's camera session (503 after)."""

    credential_encryption_key: SecretStr | None = None
    """256-bit key (base64 or hex) that encrypts the passwords of linked Mammotion accounts at
    rest (ADR 0034). Without it linked accounts are unavailable (503) and old cloud mowers keep
    their inline credentials; give it as ``MOWDECK_CREDENTIAL_ENCRYPTION_KEY_FILE`` in
    production and keep a backup: without the key the stored passwords cannot be read."""

    credential_encryption_key_previous: SecretStr | None = None
    """Comma separated earlier keys that still decrypt during a rotation; the next re-read of a
    linked account seals its password with the current key."""

    cloud_discovery_timeout_seconds: float = Field(default=50.0, ge=1, le=300)
    """How long Fleet waits for the Device-Gateway to log in to a vendor cloud account and list
    its devices (``gateway.cloud.discover``; 503 after)."""

    @model_validator(mode="after")
    def _no_dev_key_in_production(self) -> Self:
        key = self.credential_encryption_key
        if self.environment == "prod" and key is not None and key.get_secret_value().strip():
            from fleet.adapters.crypto import DEV_KEY, parse_key

            try:
                raw = parse_key(key.get_secret_value())
            except ValueError:
                raw = b""
            if raw == DEV_KEY:
                msg = (
                    "MOWDECK_CREDENTIAL_ENCRYPTION_KEY is the development key of .env.example; "
                    "generate your own for production"
                )
                raise ValueError(msg)
        return self

    def to_config(self) -> FleetConfig:
        """The use case configuration."""
        return FleetConfig(
            default_timezone=self.default_timezone,
            max_mowers_per_account=self.max_mowers_per_account,
            allowed_connection_kinds=self.connection_kinds,
            connection_options_max_bytes=self.connection_options_max_bytes,
            bulk_max_mowers=self.bulk_max_mowers,
            spot_plan_max_bytes=self.spot_plan_max_bytes,
            limits=CommandLimits(
                default_ttl=timedelta(seconds=self.command_ttl_seconds),
                max_ttl=timedelta(seconds=self.command_ttl_max_seconds),
                emergency_stop_ttl=timedelta(seconds=self.emergency_stop_ttl_seconds),
                manual_drive_ttl=timedelta(seconds=self.manual_drive_ttl_seconds),
            ),
            command_retention=timedelta(days=self.command_retention_days),
        )

    @property
    def connection_kinds(self) -> frozenset[str]:
        """``allowed_connection_kinds`` as a set."""
        return frozenset(k.strip() for k in self.allowed_connection_kinds.split(",") if k.strip())
