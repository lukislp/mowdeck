"""Service configuration from environment variables and secret files.

Every setting is read from ``MOWDECK_<NAME>`` (case insensitive). Any setting can instead be read
from a file named by ``MOWDECK_<NAME>_FILE``, which is how Docker and Kubernetes secrets are
mounted. Setting both for the same name is an error. Services subclass :class:`ServiceSettings`
to add their own settings; the same rules apply to them.

The field docstrings below are the configuration reference.
"""

import os
from pathlib import Path
from typing import Any, Literal, Self

from pydantic import Field, SecretStr, field_validator
from pydantic.fields import FieldInfo
from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
)

from mowdeck_service_kit.source import source_code_url

ENV_PREFIX = "MOWDECK_"
FILE_SUFFIX = "_FILE"

LogLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]


class SecretFileSettingsSource(PydanticBaseSettingsSource):
    """Read ``MOWDECK_<NAME>_FILE`` and use the file content (trailing newline stripped)."""

    def __init__(self, settings_cls: type[BaseSettings], env_prefix: str) -> None:
        super().__init__(settings_cls)
        self._prefix = env_prefix.upper()
        self._environ = {key.upper(): value for key, value in os.environ.items()}

    def get_field_value(self, field: FieldInfo, field_name: str) -> tuple[Any, str, bool]:
        """Return ``(value, key, is_complex)`` for one field, or ``(None, key, False)``."""
        env_name = f"{self._prefix}{field_name.upper()}"
        file_var = f"{env_name}{FILE_SUFFIX}"
        path = self._environ.get(file_var)
        if not path:
            return None, field_name, False
        if env_name in self._environ:
            msg = f"both {env_name} and {file_var} are set; use only one"
            raise ValueError(msg)
        try:
            content = Path(path).read_text(encoding="utf-8")
        except OSError as exc:
            msg = f"{file_var}: cannot read secret file {path!r}: {exc.strerror}"
            raise ValueError(msg) from exc
        return content.rstrip("\r\n"), field_name, False

    def __call__(self) -> dict[str, Any]:
        values: dict[str, Any] = {}
        for field_name, field in self.settings_cls.model_fields.items():
            value, key, _ = self.get_field_value(field, field_name)
            if value is not None:
                values[key] = value
        return values


class ServiceSettings(BaseSettings):
    """Settings shared by every MowDeck service (environment prefix ``MOWDECK_``)."""

    model_config = SettingsConfigDict(
        env_prefix=ENV_PREFIX,
        case_sensitive=False,
        extra="ignore",
        validate_default=True,
        use_attribute_docstrings=True,
    )

    service_name: str = Field(min_length=1, max_length=64, pattern=r"^[a-z][a-z0-9\-]*$")
    """Name of the service, used in logs, metrics and traces (``MOWDECK_SERVICE_NAME``)."""

    environment: Literal["dev", "test", "prod"] = "prod"
    """Deployment environment. ``dev`` enables human readable console logs by default."""

    log_level: LogLevel = "INFO"
    """Minimum log level: DEBUG, INFO, WARNING, ERROR or CRITICAL (case insensitive)."""

    log_format: Literal["json", "console", "auto"] = "auto"
    """Log output format. ``auto`` means console in ``dev`` and JSON everywhere else."""

    http_host: str = "0.0.0.0"  # noqa: S104 - services run in containers and must listen on all interfaces
    """Interface the HTTP server binds to."""

    http_port: int = Field(default=8000, ge=1, le=65535)
    """Port the HTTP server listens on."""

    nats_url: str = "nats://localhost:4222"
    """NATS server URL(s), comma separated. May contain credentials, so ``_FILE`` is supported."""

    nats_enabled: bool = True
    """Connect to NATS on startup. Disable for services without bus traffic and in tests."""

    database_url: SecretStr | None = None
    """PostgreSQL URL, e.g. ``postgresql://user:pass@host/db``. Usually given as ``_FILE``."""

    otel_exporter_otlp_endpoint: str | None = None
    """OTLP/HTTP endpoint (e.g. ``http://otel-collector:4318``). Tracing is off when unset."""

    metrics_enabled: bool = True
    """Serve Prometheus metrics at ``/metrics``."""

    shutdown_grace_seconds: float = Field(default=10.0, ge=0, le=300)
    """Time budget for shutdown hooks and draining the bus before the process exits."""

    entitlements_billing_enabled: bool = False
    """Installation switch of the subscription plans (``billing.enabled``). Off: every account
    may use everything (plan ``unlimited``). On: the plans of the Entitlements service apply,
    and a check fails closed while Entitlements does not answer (ADR 0028)."""

    entitlements_timeout_seconds: float = Field(default=1.0, gt=0, le=30)
    """Timeout of one ``entitlements.check`` request."""

    entitlements_cache_seconds: float = Field(default=60.0, ge=0, le=3600)
    """How long a service reuses an account's plan snapshot (``entitlements.plan.changed``
    drops it earlier); 0 disables the cache."""

    version: str | None = None
    """Version of the build (``MOWDECK_VERSION``, baked into the image by the release build);
    empty for a development build."""

    source_url: str | None = None
    """Repository of the running code (``MOWDECK_SOURCE_URL``, default the official one): a fork
    that runs modified code points it to its own public repository (AGPL-3.0 section 13). A
    template with ``{ref}`` is allowed (see :mod:`mowdeck_service_kit.source`)."""

    source_ref: str | None = None
    """Git ref of the build (``MOWDECK_SOURCE_REF``): ``v<version>`` for a release, a commit for
    a test build. Empty: ``v<version>`` when the version is a semantic version."""

    demo_mode: bool = False
    """Public demo installation (ADR 0068): simulated mowers only, every response marked with
    ``X-MowDeck-Demo: 1`` and the actions of the service's demo policy refused with 403. Never
    on an installation with real mowers or accounts."""

    demo_seed_token: SecretStr | None = None
    """Shared secret of the demo seed job (``MOWDECK_DEMO_SEED_TOKEN_FILE`` or the variable):
    with it in ``X-MowDeck-Demo-Seed`` a request passes the demo policy and may call the seed
    endpoints. Only in demo mode; the gateway strips the header from outside requests."""

    client_ip_depth: int | None = Field(default=None, ge=0, le=8)
    """Trusted proxies in front of the gateway that set or append ``X-Forwarded-For``
    (``MOWDECK_CLIENT_IP_DEPTH``, Compose and Helm pass ``GATEWAY_CLIENT_IP_DEPTH``): the client
    address is the entry this many places left of the gateway's own (see
    :mod:`mowdeck_service_kit.client_address`). Empty: the leftmost entry, as before."""

    @field_validator("client_ip_depth", mode="before")
    @classmethod
    def _empty_depth_is_unset(cls, value: Any) -> Any:
        return None if isinstance(value, str) and not value.strip() else value

    @field_validator("log_level", mode="before")
    @classmethod
    def _upper_log_level(cls, value: Any) -> Any:
        return value.upper() if isinstance(value, str) else value

    @property
    def resolved_log_format(self) -> Literal["json", "console"]:
        """The effective log format after resolving ``auto``."""
        if self.log_format == "auto":
            return "console" if self.environment == "dev" else "json"
        return self.log_format

    @property
    def source_code_url(self) -> str:
        """Address of the source code of this build (the ``X-Source-Code`` header)."""
        return source_code_url(self.source_url, version=self.version, ref=self.source_ref)

    @property
    def nats_servers(self) -> list[str]:
        """``nats_url`` split into a list of server URLs."""
        return [url.strip() for url in self.nats_url.split(",") if url.strip()]

    @classmethod
    def from_env(cls) -> Self:
        """Load settings from the environment and secret files.

        Use this instead of ``ServiceSettings()``: type checkers cannot know that required
        fields such as ``service_name`` come from the environment.
        """
        return cls()  # type: ignore[call-arg]

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        """Priority: explicit arguments, environment, ``_FILE`` secrets, defaults."""
        prefix = str(settings_cls.model_config.get("env_prefix", ENV_PREFIX))
        return (
            init_settings,
            env_settings,
            SecretFileSettingsSource(settings_cls, prefix),
        )
