"""Verification settings, read from ``MOWDECK_AUTH_*`` environment variables.

| Variable | Meaning |
| --- | --- |
| ``MOWDECK_AUTH_JWKS_URL`` | JWKS of Identity (``http://identity:8000/.well-known/jwks.json``) |
| ``MOWDECK_AUTH_ISSUER`` | expected ``iss``, default ``urn:mowdeck:identity`` |
| ``MOWDECK_AUTH_AUDIENCE`` | expected ``aud``, default ``mowdeck-api`` |
| ``MOWDECK_AUTH_LEEWAY_SECONDS`` | clock skew allowed for ``exp``, ``nbf``, ``iat``, default 30 |
| ``MOWDECK_AUTH_JWKS_CACHE_SECONDS`` | how long a fetched key set is used, default 300 |
| ``MOWDECK_AUTH_JWKS_REFRESH_COOLDOWN_SECONDS`` | minimum time between two fetches, default 30 |
"""

import os
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Self

ENV_PREFIX = "MOWDECK_AUTH_"
DEFAULT_ISSUER = "urn:mowdeck:identity"
"""Issuer used by Identity unless ``MOWDECK_IDENTITY_ISSUER`` says otherwise."""
DEFAULT_AUDIENCE = "mowdeck-api"
"""Audience of every MowDeck access token."""


@dataclass(frozen=True, slots=True)
class AuthSettings:
    """Where the public keys are and what a valid token must say."""

    jwks_url: str
    issuer: str = DEFAULT_ISSUER
    audience: str = DEFAULT_AUDIENCE
    leeway_seconds: float = 30.0
    jwks_cache_seconds: float = 300.0
    jwks_refresh_cooldown_seconds: float = 30.0

    def __post_init__(self) -> None:
        if not self.jwks_url.startswith(("http://", "https://")):
            msg = "jwks_url must be an http(s) URL"
            raise ValueError(msg)
        durations = (self.leeway_seconds, self.jwks_cache_seconds)
        if min(*durations, self.jwks_refresh_cooldown_seconds) < 0:
            msg = "durations must not be negative"
            raise ValueError(msg)

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> Self | None:
        """Read the settings; None when ``MOWDECK_AUTH_JWKS_URL`` is not set."""
        env = {k.upper(): v for k, v in (os.environ if environ is None else environ).items()}

        def get(name: str) -> str | None:
            value = env.get(f"{ENV_PREFIX}{name}", "").strip()
            return value or None

        url = get("JWKS_URL")
        if url is None:
            return None
        return cls(
            jwks_url=url,
            issuer=get("ISSUER") or DEFAULT_ISSUER,
            audience=get("AUDIENCE") or DEFAULT_AUDIENCE,
            leeway_seconds=float(get("LEEWAY_SECONDS") or 30),
            jwks_cache_seconds=float(get("JWKS_CACHE_SECONDS") or 300),
            jwks_refresh_cooldown_seconds=float(get("JWKS_REFRESH_COOLDOWN_SECONDS") or 30),
        )
