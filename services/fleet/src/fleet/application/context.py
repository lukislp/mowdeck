"""Configuration of the use cases and who is acting."""

from dataclasses import dataclass, field
from datetime import timedelta
from uuid import UUID

from mowdeck_contracts.v1 import Principal

from fleet.domain.rules import CommandLimits


@dataclass(frozen=True, slots=True)
class FleetConfig:
    """The settings the use cases need (built from ``Settings`` in ``main``)."""

    default_timezone: str = "Europe/Berlin"
    default_site_name: str = "Default site"
    max_mowers_per_account: int = 50
    allowed_connection_kinds: frozenset[str] = frozenset({"simulated"})
    connection_options_max_bytes: int = 4096
    bulk_max_mowers: int = 200
    spot_plan_max_bytes: int = 262_144
    limits: CommandLimits = field(default_factory=CommandLimits)
    command_retention: timedelta = timedelta(days=90)
    status_url_prefix: str = "/api/v1/fleet/commands/"


@dataclass(frozen=True, slots=True)
class Actor:
    """Who acts: a verified principal of the account, or the system (events, jobs)."""

    principal: Principal | None = None
    correlation_id: str | None = None

    @property
    def subject_id(self) -> UUID | None:
        """The principal's subject id (None for the system)."""
        return self.principal.subject if self.principal else None

    @property
    def subject(self) -> str | None:
        """``user:<id>`` or ``api_token:<id>`` for the audit log."""
        if self.principal is None:
            return None
        prefix = "api_token" if self.principal.kind.value == "api_token" else "user"
        return f"{prefix}:{self.principal.subject}"


SYSTEM = Actor()
"""Background jobs and event handlers."""
