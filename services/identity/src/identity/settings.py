"""Settings of the identity service.

Every field is read from ``MOWDECK_<NAME>`` or from the file named by ``MOWDECK_<NAME>_FILE``.
The field docstrings are the configuration reference (README settings table). OpenFGA is
configured with the ``MOWDECK_OPENFGA_*`` variables of ``mowdeck-authz``.
"""

from datetime import timedelta
from typing import Literal

from mowdeck_auth import DEFAULT_AUDIENCE, DEFAULT_ISSUER
from mowdeck_service_kit import ServiceSettings, get_logger
from pydantic import Field, SecretStr

from identity.application.context import GitHubLogin, IdentityConfig, RegistrationMode
from identity.domain.models import RelyingParty, oauth_callback_url

_log = get_logger("identity.settings")


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

    identity_public_url: str = ""
    """The address users open MowDeck at, e.g. ``https://mowdeck.example.org``. Passkeys are
    bound to it (WebAuthn relying party: its host is the RP ID, its origin the only accepted
    origin); empty turns passkeys off. Needs HTTPS (plain HTTP only for ``localhost``) and a host
    name, not an IP address. Changing the host later makes existing passkeys unusable."""

    identity_passkey_rp_id: str = ""
    """RP ID of the passkeys when it should be a parent domain of the host of the public URL
    (e.g. ``example.org``); empty: that host."""

    identity_passkey_rp_name: str = Field(default="MowDeck", max_length=64)
    """Name of the relying party that authenticators show next to the passkey."""

    identity_github_client_id: str = Field(default="", max_length=100)
    """Client ID of the GitHub OAuth App for "Sign in with GitHub" (ADR 0051). Empty: GitHub
    login is off. Needs the client secret and ``MOWDECK_IDENTITY_PUBLIC_URL`` (the callback is
    ``<public URL>/api/v1/identity/oauth/github/callback``)."""

    identity_github_client_secret: SecretStr | None = None
    """Client secret of that OAuth App; give it as ``MOWDECK_IDENTITY_GITHUB_CLIENT_SECRET_FILE``
    (a Docker or Kubernetes secret). Never in the database, never shown in the web app."""

    identity_github_authorize_url: str = "https://github.com/login/oauth/authorize"
    """GitHub's authorization page (another value only for GitHub Enterprise Server or tests)."""

    identity_github_token_url: str = "https://github.com/login/oauth/access_token"  # noqa: S105
    """GitHub's token endpoint (another value only for GitHub Enterprise Server or tests)."""

    identity_github_api_url: str = "https://api.github.com"
    """GitHub's REST API (``https://<host>/api/v3`` for GitHub Enterprise Server)."""

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
            passkey_rp=self.relying_party(),
        )

    def relying_party(self) -> RelyingParty | None:
        """The WebAuthn relying party from the public URL; None (passkeys off) when it is not
        set or not usable, which is logged instead of stopping the service: a wrong value must
        not lock everyone out of the password login."""
        if not self.identity_public_url.strip():
            return None
        try:
            return RelyingParty.from_public_url(
                self.identity_public_url,
                rp_id=self.identity_passkey_rp_id,
                name=self.identity_passkey_rp_name,
            )
        except ValueError as exc:
            _log.error(
                "passkeys are off: MOWDECK_IDENTITY_PUBLIC_URL is not usable", reason=str(exc)
            )
            return None

    def github_login(self) -> GitHubLogin | None:
        """The GitHub login settings, or None (off). Logged once at startup: on with the callback
        URL to register, or why it is off; a wrong value never stops the service."""
        client_id = self.identity_github_client_id.strip()
        secret = self.identity_github_client_secret
        secret_value = secret.get_secret_value().strip() if secret is not None else ""
        if not client_id and not secret_value:
            return None
        problem = None
        if not client_id:
            problem = "MOWDECK_IDENTITY_GITHUB_CLIENT_ID is not set"
        elif not secret_value:
            problem = "the client secret (MOWDECK_IDENTITY_GITHUB_CLIENT_SECRET_FILE) is not set"
        callback = ""
        if problem is None:
            try:
                callback = oauth_callback_url(self.identity_public_url, "github")
            except ValueError as exc:
                problem = str(exc)
        if problem is not None:
            _log.error("GitHub login is off: the configuration is incomplete", reason=problem)
            return None
        _log.info("GitHub login is on", callback_url=callback)
        return GitHubLogin(
            client_id=client_id,
            client_secret=secret_value,
            authorize_url=self.identity_github_authorize_url,
            token_url=self.identity_github_token_url,
            api_url=self.identity_github_api_url,
            redirect_uri=callback,
        )

    @property
    def module_urls(self) -> list[str]:
        """``identity_module_urls`` as a list."""
        return [u.strip() for u in self.identity_module_urls.split(",") if u.strip()]
