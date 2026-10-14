"""Settings of the entitlements service.

Every field is read from ``MOWDECK_<NAME>`` or from the file named by ``MOWDECK_<NAME>_FILE``.
The field docstrings are the configuration reference (README settings table). The installation
switch ``MOWDECK_ENTITLEMENTS_BILLING_ENABLED`` is a kit setting (every service reads it for its
fallback); OpenFGA is configured with the ``MOWDECK_OPENFGA_*`` variables of ``mowdeck-authz``,
token verification with the ``MOWDECK_AUTH_*`` variables of ``mowdeck-auth``.
"""

from mowdeck_service_kit import ServiceSettings
from pydantic import Field

from entitlements.application.entitlements import EntitlementsConfig


class Settings(ServiceSettings):
    """Settings of entitlements: the kit settings plus service defaults."""

    service_name: str = Field(
        default="entitlements", min_length=1, max_length=64, pattern=r"^[a-z][a-z0-9\-]*$"
    )
    """Name of the service in logs, metrics and traces."""

    http_port: int = Field(default=8000, ge=1, le=65535)
    """Port the HTTP server listens on."""

    entitlements_default_plan: str = Field(default="home", min_length=1, max_length=64)
    """Plan of accounts without an assignment while billing is enabled (and of new accounts).
    When no plan has this key, the plan marked ``is_default`` in the plan file applies."""

    entitlements_plans_file: str = "/etc/mowdeck/entitlements/plans.yaml"
    """The plan file seeded at startup (the image ships ``infra/entitlements/plans.yaml`` at
    this path). A missing file only leaves the built-in plan ``unlimited``; an invalid file
    stops the start."""

    entitlements_grace_days: int = Field(default=14, ge=0, le=365)
    """Length of a grace period assigned without ``grace_until``."""

    entitlements_module_urls: str = ""
    """Comma separated base URLs of the services whose module descriptors declare features,
    e.g. ``http://fleet:8000,http://realtime:8000``."""

    entitlements_catalogue_refresh_seconds: float = Field(default=600, ge=10)
    """How often the module descriptors are fetched again (30 s while one is missing)."""

    def to_config(self) -> EntitlementsConfig:
        """The use case configuration."""
        return EntitlementsConfig(
            billing_enabled=self.entitlements_billing_enabled,
            default_plan=self.entitlements_default_plan,
            grace_days=self.entitlements_grace_days,
        )

    @property
    def module_urls(self) -> list[str]:
        """``entitlements_module_urls`` as a list."""
        return [u.strip() for u in self.entitlements_module_urls.split(",") if u.strip()]
