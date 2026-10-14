"""Connection settings for OpenFGA, read from ``MOWDECK_OPENFGA_*`` environment variables.

| Variable | Meaning |
| --- | --- |
| ``MOWDECK_OPENFGA_API_URL`` | HTTP API, e.g. ``http://openfga:8080`` (required) |
| ``MOWDECK_OPENFGA_API_TOKEN`` or ``MOWDECK_OPENFGA_API_TOKEN_FILE`` | one of the pre-shared keys |
| ``MOWDECK_OPENFGA_STORE_NAME`` | store to resolve by name, default ``mowdeck`` |
| ``MOWDECK_OPENFGA_STORE_ID`` | optional pin; skips the lookup by name |
| ``MOWDECK_OPENFGA_MODEL_ID`` | optional pin; otherwise the latest model is pinned at connect |
| ``MOWDECK_OPENFGA_CACHE_TTL_SECONDS`` | decision cache lifetime, default 5, 0 disables |
| ``MOWDECK_OPENFGA_TIMEOUT_SECONDS`` | per request timeout, default 2 |
"""

import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Self

ENV_PREFIX = "MOWDECK_OPENFGA_"
DEFAULT_STORE_NAME = "mowdeck"


@dataclass(frozen=True, slots=True)
class AuthzSettings:
    """How to reach OpenFGA and which store and model to use."""

    api_url: str
    api_token: str | None = field(default=None, repr=False)
    store_name: str = DEFAULT_STORE_NAME
    store_id: str | None = None
    model_id: str | None = None
    cache_ttl_seconds: float = 5.0
    cache_max_entries: int = 10_000
    timeout_seconds: float = 2.0

    def __post_init__(self) -> None:
        if not self.api_url:
            msg = "api_url is required"
            raise ValueError(msg)
        if self.cache_ttl_seconds < 0 or self.timeout_seconds <= 0:
            msg = "cache_ttl_seconds must be >= 0 and timeout_seconds > 0"
            raise ValueError(msg)

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> Self:
        """Read the settings from the environment (``_FILE`` supported for the token)."""
        env = {k.upper(): v for k, v in (os.environ if environ is None else environ).items()}

        def get(name: str) -> str | None:
            value = env.get(f"{ENV_PREFIX}{name}", "").strip()
            return value or None

        api_url = get("API_URL")
        if api_url is None:
            msg = f"{ENV_PREFIX}API_URL is required"
            raise ValueError(msg)
        token = get("API_TOKEN")
        token_file = get("API_TOKEN_FILE")
        if token and token_file:
            msg = f"both {ENV_PREFIX}API_TOKEN and {ENV_PREFIX}API_TOKEN_FILE are set"
            raise ValueError(msg)
        if token_file:
            token = Path(token_file).read_text(encoding="utf-8").rstrip("\r\n")
        ttl = get("CACHE_TTL_SECONDS")
        timeout = get("TIMEOUT_SECONDS")
        return cls(
            api_url=api_url,
            api_token=token,
            store_name=get("STORE_NAME") or DEFAULT_STORE_NAME,
            store_id=get("STORE_ID"),
            model_id=get("MODEL_ID"),
            cache_ttl_seconds=float(ttl) if ttl is not None else 5.0,
            timeout_seconds=float(timeout) if timeout is not None else 2.0,
        )
