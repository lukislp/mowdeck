# SPDX-FileCopyrightText: 2026 Lukas Körber
# SPDX-License-Identifier: AGPL-3.0-only

"""Configuration of the use cases and the per-request context (who, from where, which trace)."""

from dataclasses import dataclass, field
from datetime import timedelta
from enum import StrEnum

from cryptography.hazmat.primitives.asymmetric.ec import EllipticCurvePrivateKey
from mowdeck_contracts.v1 import Principal

from identity.domain.models import AuditEntry, RelyingParty


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
    passkey_rp: RelyingParty | None = None
    """The WebAuthn relying party; None turns passkeys off (no public URL configured)."""
    webauthn_timeout: timedelta = timedelta(minutes=5)
    """How long a registration or login ceremony may take (the challenge lives as long)."""
    app_code_ttl: timedelta = timedelta(seconds=60)
    """Lifetime of a one-time code that hands a browser login to the native app."""
    app_request_ttl: timedelta = timedelta(minutes=10)
    """How long the app's sign-in request (the system browser page) stays usable."""
    app_handoff_login_age: timedelta = timedelta(minutes=5)
    """Only a browser login session younger than this can be handed to the app."""
    demo_username: str | None = None
    """Public demo (ADR 0068): the prepared user every visitor signs in as with one click.
    None outside demo mode, where the one-click sign-in does not exist."""

    @property
    def demo(self) -> bool:
        """Public demo mode: the one predicate for "this is the public demo" in Identity."""
        return self.demo_username is not None

    def request_context(
        self,
        *,
        principal: Principal | None = None,
        ip: str | None = None,
        correlation_id: str | None = None,
        user_agent: str | None = None,
        extra: dict[str, str] | None = None,
    ) -> "RequestContext":
        """The context of one request. The only place that decides whether the visitor's address
        and browser may be written down: the public demo keeps neither (see
        :attr:`RequestContext.stored_ip`)."""
        return RequestContext(
            principal=principal,
            ip=ip,
            correlation_id=correlation_id,
            user_agent=user_agent,
            extra=extra or {},
            keep_visitor_data=not self.demo,
        )


@dataclass(frozen=True, slots=True)
class GitHubLogin:
    """Settings of "Sign in with GitHub" (ADR 0051); only server side, never shown anywhere."""

    client_id: str
    client_secret: str = field(repr=False)
    authorize_url: str
    token_url: str
    api_url: str
    redirect_uri: str
    """``<public URL>/api/v1/identity/oauth/github/callback``, registered in the OAuth App."""


@dataclass(frozen=True, slots=True)
class AppleLogin:
    """Settings of "Sign in with Apple" (ADR 0052); only server side, never shown anywhere."""

    services_id: str
    team_id: str
    key_id: str
    private_key: EllipticCurvePrivateKey = field(repr=False)
    redirect_uri: str
    """``<public URL>/api/v1/identity/oauth/apple/callback``, registered at the Services ID."""
    authorize_url: str
    token_url: str
    keys_url: str
    issuer: str
    pkce: bool


@dataclass(frozen=True, slots=True)
class RequestContext:
    """Who acts (None before login), from which address, under which correlation id."""

    principal: Principal | None = None
    ip: str | None = None
    correlation_id: str | None = None
    user_agent: str | None = None
    extra: dict[str, str] = field(default_factory=dict)
    keep_visitor_data: bool = True
    """False in the public demo: ``ip`` and ``user_agent`` still serve the in-memory rate limit
    and the response, but nothing persists them (use ``stored_ip`` and ``stored_user_agent``
    for every row, audit entry and device record)."""

    @property
    def stored_ip(self) -> str | None:
        """The address to persist: None when the context must not keep visitor data."""
        return self.ip if self.keep_visitor_data else None

    @property
    def stored_user_agent(self) -> str | None:
        """The browser string to persist (cut to the column width): None when the context must
        not keep visitor data, and None for an empty one."""
        return (self.user_agent or "")[:200] or None if self.keep_visitor_data else None

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
            "ip": self.stored_ip,
            "correlation_id": self.correlation_id,
        }
        return AuditEntry(**{**base, **values})  # type: ignore[arg-type]


SYSTEM = RequestContext()
"""Context of background jobs and the CLI (no principal)."""
