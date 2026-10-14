"""Settings of the identity service.

Every field is read from ``MOWDECK_<NAME>`` or from the file named by ``MOWDECK_<NAME>_FILE``.
The field docstrings are the configuration reference (README settings table). OpenFGA is
configured with the ``MOWDECK_OPENFGA_*`` variables of ``mowdeck-authz``.
"""

from datetime import timedelta
from typing import Literal

from mowdeck_auth import DEFAULT_AUDIENCE, DEFAULT_ISSUER
from mowdeck_service_kit import ServiceSettings
from pydantic import Field, SecretStr

from identity.application.context import IdentityConfig, RegistrationMode


class Settings(ServiceSettings):
    """Settings of identity: the kit settings plus service defaults."""

    service_name: str = Field(
        default="identity", min_length=1, max_length=64, pattern=r"^[a-z][a-z0-9\-]*$"
    )
    """Name of the service in logs, metrics and traces."""

    http_port: int = Field(default=8000, ge=1, le=65535)
    """Port the HTTP server listens on."""

    identity_registration: RegistrationMode = RegistrationMode.OPEN
    """``open``: anybody may register (home installation default). ``closed``: only the first
    account of the installation, then 403. ``invite``: reserved, behaves like ``closed``."""

    identity_issuer: str = DEFAULT_ISSUER
    """``iss`` of issued tokens; services set ``MOWDECK_AUTH_ISSUER`` to the same value."""

    identity_audience: str = DEFAULT_AUDIENCE
    """``aud`` of issued tokens."""

    identity_access_token_minutes: int = Field(default=10, ge=1, le=15)
    """Lifetime of access tokens (the plan allows 5 to 15 minutes)."""

    identity_refresh_idle_days: int = Field(default=7, ge=1, le=90)
    """A refresh token unused for this long expires."""

    identity_refresh_absolute_days: int = Field(default=30, ge=1, le=365)
    """A login session ends after this long, however active."""

    identity_refresh_reuse_grace_seconds: int = Field(default=10, ge=0, le=60)
    """A rotated refresh token presented again within this many seconds, while its successor
    is still unused, counts as a lost response and gets a new token instead of ending the login
    session (0 disables the grace)."""

    identity_key_encryption_key: SecretStr | None = None
    """256-bit key (base64 or hex) that encrypts the signing keys at rest. Required; give it as
    ``MOWDECK_IDENTITY_KEY_ENCRYPTION_KEY_FILE`` in production."""

    identity_key_rotation_days: int = Field(default=30, ge=1, le=365)
    """Age of the active signing key after which the next key is announced and then promoted."""

    identity_module_urls: str = ""
    """Comma separated base URLs of the services whose module descriptors define permissions,
    e.g. ``http://device-gateway:8000``."""

    identity_catalogue_refresh_seconds: float = Field(default=600, ge=10)
    """How often the module descriptors are fetched again."""

    identity_argon2_profile: Literal["rfc9106_low", "owasp_min"] = "rfc9106_low"
    """Argon2id parameters: ``rfc9106_low`` (64 MiB, t=3, p=4) or ``owasp_min`` (19 MiB, t=2,
    p=1) for small hosts."""

    identity_argon2_concurrency: int = Field(default=4, ge=1, le=64)
    """Password hashes computed at the same time (memory: concurrency x profile memory)."""

    identity_login_ip_per_minute: float = Field(default=10, gt=0)
    """Login and token exchange requests per client IP and minute (after the burst)."""

    identity_login_ip_burst: int = Field(default=20, ge=1)
    """Requests a client IP may send at once before the rate applies."""

    identity_login_max_failures: int = Field(default=10, ge=1)
    """Failed logins per username within the window before the username is throttled (429)."""

    identity_login_failure_window_seconds: int = Field(default=900, ge=60)
    """Window of the per username failure count."""

    identity_trust_forwarded_for: bool = False
    """Take the client IP from ``X-Forwarded-For`` (set it only behind a trusted proxy such as
    the Compose Traefik)."""

    identity_deletion_grace_days: int = Field(default=7, ge=0, le=90)
    """Days an account stays locked after a deletion request before it may be purged."""

    identity_default_plan: str = "unlimited"
    """Plan key of new accounts while billing is off."""

    identity_refresh_cookie_secure: bool = True
    """Send the refresh cookie only over HTTPS. Disable only for plain HTTP development."""

    identity_github_login: bool = False
    """Feature flag of the GitHub login (not implemented in Phase 1; must stay off)."""

    identity_maintenance_seconds: float = Field(default=60, ge=1)
    """Interval of the maintenance loop (key rotation, key reload, attempt purge)."""

    identity_reconcile_seconds: float = Field(default=3600, ge=0)
    """Interval of the full OpenFGA reconcile; 0 disables it."""

    def to_config(self) -> IdentityConfig:
        """The use case configuration."""
        return IdentityConfig(
            registration=self.identity_registration,
            default_plan=self.identity_default_plan,
            access_token_ttl=timedelta(minutes=self.identity_access_token_minutes),
            refresh_idle=timedelta(days=self.identity_refresh_idle_days),
            refresh_absolute=timedelta(days=self.identity_refresh_absolute_days),
            refresh_reuse_grace=timedelta(seconds=self.identity_refresh_reuse_grace_seconds),
            deletion_grace=timedelta(days=self.identity_deletion_grace_days),
            login_failure_window=timedelta(seconds=self.identity_login_failure_window_seconds),
            login_max_failures=self.identity_login_max_failures,
            key_rotation=timedelta(days=self.identity_key_rotation_days),
        )

    @property
    def module_urls(self) -> list[str]:
        """``identity_module_urls`` as a list."""
        return [u.strip() for u in self.identity_module_urls.split(",") if u.strip()]
