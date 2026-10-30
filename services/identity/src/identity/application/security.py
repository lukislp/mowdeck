"""The security center of a user (ADR 0065): sessions and devices, security events, alerts.

* **Sessions.** A login session is a refresh token family (``sessions.family_id``, the ``sid``
  of the access tokens). The list shows every live family of the caller, in every account they
  belong to: the device from the newest user agent, the address shortened to its network, when
  it started (the first row), when it was last used (the newest row) and how the user signed in.
  Nobody else sees them: there is no admin view of another user's sessions here.
* **Revoking** marks every row of the family revoked: the next refresh fails at once. Access
  tokens are verified offline by every service and live at most ``access_token_ttl`` (10
  minutes); Identity's own endpoints refuse a token of an ended session at once
  (:meth:`SecurityService.session_ended`). Revoking another session and "sign out everywhere
  else" need a recent step-up (the route checks it), so a stolen session cannot throw the owner
  out.
* **Security events** are the caller's own audit rows of the last days: logins and failed
  logins, new devices, passkeys, linked providers, password changes, personal access tokens
  and ended sessions. Audit rows are written once per account of the user for some actions;
  the list shows each event once.
* **New-login alerts** (``users.new_login_alerts``, on by default): see
  :mod:`identity.application.devices`. Turning them off needs a recent step-up.
* **Public demo** (ADR 0068): every visitor signs in as the one demo user, so "the caller's
  sessions" would be every visitor's browser, network and times. In demo mode the list, the
  events and the overview's session count only cover the caller's own login session.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID

from mowdeck_contracts.v1 import Principal, PrincipalKind

from identity.application.context import RequestContext
from identity.application.deps import Deps
from identity.application.ports import Ge, In
from identity.domain.devices import DeviceInfo, NetworkKind, describe_user_agent, ip_prefix
from identity.domain.errors import ForbiddenError, NotFoundError
from identity.domain.models import AuditEntry, Session, TokenPurpose

EVENT_DAYS_MAX = 90
"""Longest period the security events reach back."""
EVENT_LIMIT_MAX = 200

TARGET_ACTIONS: tuple[str, ...] = (
    "session.login",
    "session.login_failed",
    "session.logout_all",
    "session.refresh_reuse",
    "session.revoked",
    "session.revoked_others",
    "security.new_device",
    "security.alerts_changed",
    "user.password_changed",
    "user.password_reset",
    "passkey.added",
    "passkey.renamed",
    "passkey.removed",
    "passkey.reauth_failed",
    "external_identity.linked",
    "external_identity.unlinked",
    "external_identity.reauth_failed",
    "step_up.failed",
    "step_up.reauth_failed",
)
"""Actions about the user (``target_id`` is the user) shown as security events."""

ACTOR_ACTIONS: tuple[str, ...] = (
    "api_token.created",
    "api_token.personal_created",
    "api_token.widget_created",
    "api_token.revoked",
)
"""Actions the user did to a token (``actor_subject`` is the user)."""


@dataclass(frozen=True, slots=True)
class SessionView:
    """One live login session of the caller."""

    id: UUID
    """The family id (the ``sid`` of its access tokens)."""
    account_id: UUID
    current: bool
    device: DeviceInfo
    ip_prefix: str | None
    network: NetworkKind
    created_at: datetime
    last_used_at: datetime
    expires_at: datetime
    method: str | None
    """``pwd``, ``passkey``, ``github``, ``apple`` (how the session was started)."""


@dataclass(frozen=True, slots=True)
class SecurityOverview:
    """The figures at the top of the security page."""

    password_set: bool
    password_changed_at: datetime | None
    must_change_password: bool
    new_login_alerts: bool
    passkeys: int
    providers: list[str]
    personal_tokens: int
    """Active (not revoked, not expired) personal access tokens in the current account."""
    sessions: int
    known_devices: int


def _live(row: Session, now: datetime) -> bool:
    return (
        row.revoked_at is None
        and row.replaced_by is None
        and now < row.idle_expires_at
        and now < row.absolute_expires_at
    )


def _event_key(entry: AuditEntry) -> tuple[Any, ...]:
    """One event per action, moment, target and request (rows per account collapse)."""
    return (entry.action, entry.at, entry.target_id, entry.correlation_id, entry.outcome)


class SecurityService:
    """Use cases of the security page; everything is the caller's own."""

    def __init__(self, deps: Deps) -> None:
        self.deps = deps

    @property
    def _demo(self) -> bool:
        """Demo mode: all visitors share one user, so only the own session may be shown."""
        return self.deps.config.demo_username is not None

    @staticmethod
    def _user(ctx: RequestContext) -> Principal:
        principal = ctx.principal
        if principal is None or principal.kind not in (PrincipalKind.USER, PrincipalKind.SUBUSER):
            msg = "only users have a security page"
            raise ForbiddenError(msg)
        return principal

    # --- overview ----------------------------------------------------------------------------

    async def overview(self, ctx: RequestContext) -> SecurityOverview:
        """Password status, alert setting and the counts of the caller."""
        principal = self._user(ctx)
        now = self.deps.clock()
        async with self.deps.store.transaction() as tx:
            found = await tx.users.get(id=principal.subject)
            if found is None:
                msg = "user not found"
                raise NotFoundError(msg)
            passkeys = await tx.passkeys.count(user_id=found.id)
            links = await tx.external_identities.find(user_id=found.id, order_by="provider")
            tokens = await tx.api_tokens.find(
                created_by=found.id,
                account_id=principal.account_id,
                purpose=TokenPurpose.PERSONAL,
                revoked_at=None,
            )
            heads = await tx.sessions.find(user_id=found.id, revoked_at=None, replaced_by=None)
            devices = await tx.known_devices.count(user_id=found.id)
        return SecurityOverview(
            password_set=found.password_hash is not None,
            password_changed_at=found.password_changed_at,
            must_change_password=found.must_change_password,
            new_login_alerts=found.new_login_alerts,
            passkeys=passkeys,
            providers=[link.provider for link in links],
            personal_tokens=sum(1 for t in tokens if t.is_usable(now)),
            sessions=len(
                {
                    row.family_id
                    for row in heads
                    if _live(row, now) and (not self._demo or row.family_id == principal.session_id)
                }
            ),
            known_devices=devices,
        )

    async def set_alerts(self, ctx: RequestContext, enabled: bool) -> bool:
        """Turn the new-login alerts on or off (audited ``security.alerts_changed``). The route
        asks for a recent step-up before turning them off."""
        principal = self._user(ctx)
        async with self.deps.uow() as work:
            found = await work.tx.users.get(id=principal.subject)
            if found is None:
                msg = "user not found"
                raise NotFoundError(msg)
            if found.new_login_alerts != enabled:
                found.new_login_alerts = enabled
                await work.tx.users.update(found)
                await work.audit(
                    ctx,
                    "security.alerts_changed",
                    account_id=principal.account_id,
                    target=("user", found.id),
                    details={"new_login_alerts": enabled},
                )
        return enabled

    # --- sessions ----------------------------------------------------------------------------

    async def sessions(self, ctx: RequestContext) -> list[SessionView]:
        """The caller's live login sessions, the current one first, then by last use."""
        principal = self._user(ctx)
        now = self.deps.clock()
        views: list[SessionView] = []
        async with self.deps.store.transaction() as tx:
            heads = await tx.sessions.find(
                user_id=principal.subject, revoked_at=None, replaced_by=None
            )
            for head in heads:
                if not _live(head, now):
                    continue
                if self._demo and head.family_id != principal.session_id:
                    continue
                first = await tx.sessions.find(
                    family_id=head.family_id, order_by="created_at", limit=1
                )
                start = first[0] if first else head
                prefix, network = ip_prefix(head.ip)
                views.append(
                    SessionView(
                        id=head.family_id,
                        account_id=head.account_id,
                        current=head.family_id == principal.session_id,
                        device=describe_user_agent(head.user_agent or start.user_agent),
                        ip_prefix=prefix,
                        network=network,
                        created_at=start.created_at,
                        last_used_at=head.last_used_at or head.created_at,
                        expires_at=min(head.idle_expires_at, head.absolute_expires_at),
                        method=start.auth_method,
                    )
                )
        return sorted(views, key=lambda v: (not v.current, -v.last_used_at.timestamp()))

    async def revoke(self, ctx: RequestContext, session_id: UUID) -> None:
        """End one of the caller's login sessions (idempotent; 404 for any other session)."""
        principal = self._user(ctx)
        now = self.deps.clock()
        async with self.deps.uow() as work:
            rows = await work.tx.sessions.find(family_id=session_id, user_id=principal.subject)
            if not rows:
                msg = "session not found"
                raise NotFoundError(msg)
            live = [row for row in rows if row.revoked_at is None]
            for row in live:
                row.revoked_at, row.revoke_reason = now, "revoked in the security center"
                await work.tx.sessions.update(row)
            if live:
                await work.audit(
                    ctx,
                    "session.revoked",
                    account_id=principal.account_id,
                    target=("user", principal.subject),
                    details={
                        "session": str(session_id),
                        "current": session_id == principal.session_id,
                    },
                )

    async def revoke_others(self, ctx: RequestContext) -> int:
        """End every login session of the caller except the current one; returns how many."""
        principal = self._user(ctx)
        now = self.deps.clock()
        async with self.deps.uow() as work:
            rows = [
                row
                for row in await work.tx.sessions.find(user_id=principal.subject, revoked_at=None)
                if row.family_id != principal.session_id
            ]
            ended = {row.family_id for row in rows if _live(row, now)}
            for row in rows:
                row.revoked_at, row.revoke_reason = now, "signed out everywhere else"
                await work.tx.sessions.update(row)
            await work.audit(
                ctx,
                "session.revoked_others",
                account_id=principal.account_id,
                target=("user", principal.subject),
                details={"sessions": len(ended)},
            )
        return len(ended)

    async def session_ended(self, principal: Principal) -> bool:
        """True when the access token belongs to a login session that was ended (revoked,
        logged out, handed to the app): Identity's own endpoints refuse it at once, other
        services accept it until it expires. Tokens without a session (API tokens) and
        sessions Identity does not know are not ended."""
        if principal.session_id is None or principal.kind not in (
            PrincipalKind.USER,
            PrincipalKind.SUBUSER,
        ):
            return False
        async with self.deps.store.transaction() as tx:
            if await tx.sessions.count(family_id=principal.session_id, revoked_at=None):
                return False
            return await tx.sessions.count(family_id=principal.session_id) > 0

    # --- events ------------------------------------------------------------------------------

    async def events(
        self, ctx: RequestContext, days: int = 30, limit: int = 100
    ) -> list[AuditEntry]:
        """The caller's security events of the last ``days``, newest first."""
        principal = self._user(ctx)
        since = self.deps.clock() - timedelta(days=max(1, min(days, EVENT_DAYS_MAX)))
        limit = max(1, min(limit, EVENT_LIMIT_MAX))
        async with self.deps.store.transaction() as tx:
            about = await tx.audit.find(
                target_id=str(principal.subject),
                action=In(TARGET_ACTIONS),
                at=Ge(since),
                order_by="at",
                descending=True,
                limit=limit * 2,
            )
            done = await tx.audit.find(
                actor_subject=f"user:{principal.subject}",
                action=In(ACTOR_ACTIONS),
                at=Ge(since),
                order_by="at",
                descending=True,
                limit=limit * 2,
            )
        seen: set[tuple[Any, ...]] = set()
        unique: list[AuditEntry] = []
        for entry in sorted([*about, *done], key=lambda e: (e.at, e.id or 0), reverse=True):
            if self._demo and (entry.details or {}).get("session") != str(principal.session_id):
                continue
            key = _event_key(entry)
            if key in seen:
                continue
            seen.add(key)
            unique.append(entry)
        return unique[:limit]
