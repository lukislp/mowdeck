"""Configuration of the use cases and the per-request context (who, from where, which trace)."""

from dataclasses import dataclass, field
from datetime import timedelta
from enum import StrEnum

from mowdeck_contracts.v1 import Principal

from identity.domain.models import AuditEntry


class RegistrationMode(StrEnum):
    """Who may register an account."""

    OPEN = "open"
    """Anybody (home installation default, cloud)."""
    CLOSED = "closed"
    """Only the very first account of the installation (bootstrap), then 403."""
    INVITE = "invite"
    """Reserved for invitations (later); behaves like ``closed`` for now."""


@dataclass(frozen=True, slots=True)
class IdentityConfig:
    """The settings the use cases need (built from ``Settings`` in ``main``)."""

    registration: RegistrationMode = RegistrationMode.OPEN
    default_plan: str = "unlimited"
    access_token_ttl: timedelta = timedelta(minutes=10)
    refresh_idle: timedelta = timedelta(days=7)
    refresh_absolute: timedelta = timedelta(days=30)
    refresh_reuse_grace: timedelta = timedelta(seconds=10)
    deletion_grace: timedelta = timedelta(days=7)
    login_failure_window: timedelta = timedelta(minutes=15)
    login_max_failures: int = 10
    api_token_touch_interval: timedelta = timedelta(minutes=1)
    key_rotation: timedelta = timedelta(days=30)
    key_promote_after: timedelta = timedelta(hours=1)
    key_retire_after: timedelta = timedelta(hours=1)
    login_attempt_retention: timedelta = timedelta(days=30)


@dataclass(frozen=True, slots=True)
class RequestContext:
    """Who acts (None before login), from which address, under which correlation id."""

    principal: Principal | None = None
    ip: str | None = None
    correlation_id: str | None = None
    user_agent: str | None = None
    extra: dict[str, str] = field(default_factory=dict)

    @property
    def actor_subject(self) -> str | None:
        """``user:<id>`` or ``api_token:<id>`` of the actor."""
        if self.principal is None:
            return None
        prefix = "api_token" if self.principal.kind.value == "api_token" else "user"
        return f"{prefix}:{self.principal.subject}"

    def audit_entry(self, **values: object) -> AuditEntry:
        """An :class:`AuditEntry` pre-filled with actor, ip and correlation id."""
        base: dict[str, object] = {
            "actor_subject": self.actor_subject,
            "actor_kind": self.principal.kind.value if self.principal else None,
            "actor_name": self.principal.display_name if self.principal else None,
            "ip": self.ip,
            "correlation_id": self.correlation_id,
        }
        return AuditEntry(**{**base, **values})  # type: ignore[arg-type]


SYSTEM = RequestContext()
"""Context of background jobs and the CLI (no principal)."""
