"""Accounts at external login providers (GitHub) linked to users (ADR 0051).

* Only explicit links: a signed-in user links their provider account in the settings (the
  current password again, then the round trip to the provider). A login through a provider
  account that is not linked is refused; there is no registration through a provider and
  nothing is ever matched or linked by e-mail address.
* A provider account belongs to at most one user, a user has at most one account per provider.
* The password always stays. Unlinking is always allowed (no password needed: it only removes a
  way in).
* The provider's callback (``finish``) ends a login (a session as after a password login) or a
  link, depending on how the round trip was started.
* Audit: ``external_identity.linked``, ``external_identity.unlinked``,
  ``external_identity.link_failed``, ``external_identity.reauth_failed``; logins write
  ``session.login`` / ``session.login_failed`` with the provider as ``method``.
"""

import re
from dataclasses import dataclass
from uuid import UUID, uuid4

from mowdeck_contracts.v1 import Principal, PrincipalKind

from identity.application.authenticators import RedirectCallback, RedirectFailure, RedirectStart
from identity.application.context import RequestContext
from identity.application.deps import Deps
from identity.application.ports import Tx
from identity.application.ratelimit import TokenBucketLimiter
from identity.application.reauth import Reauthenticator
from identity.application.sessions import SessionService, TokenPair
from identity.domain.errors import (
    AuthenticationFailed,
    ConflictError,
    ForbiddenError,
    NotFoundError,
    RateLimited,
)
from identity.domain.models import (
    AuditEntry,
    ExternalIdentity,
    RedirectPurpose,
    Status,
    User,
    UserKind,
)

PROVIDER_PATTERN = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")

TAKEN = "taken"
"""Link failure: the provider account is linked to another user."""
ALREADY = "already"
"""Link failure: the user already has another account of this provider linked."""
RATE_LIMITED = "rate_limited"
"""Callback refused by the login limit of the address."""


@dataclass(frozen=True, slots=True)
class RedirectOutcome:
    """Where a provider's callback leads: a new session (login), a new link, or a failure.

    ``failure`` is a public code the web app shows a message for (``cancelled``, ``expired``,
    ``denied``, ``unavailable``, ``taken``, ``already``, ``rate_limited``).
    """

    provider: str
    purpose: RedirectPurpose
    pair: TokenPair | None = None
    linked: ExternalIdentity | None = None
    failure: str | None = None
    return_query: str | None = None


class ExternalIdentityService:
    """Use cases around linked provider accounts and the provider callback."""

    def __init__(
        self, deps: Deps, sessions: SessionService, ip_limiter: TokenBucketLimiter | None = None
    ) -> None:
        self.deps = deps
        self.sessions = sessions
        self.reauth = Reauthenticator(deps, ip_limiter)
        """The password check before a link (login limits of the user and the address)."""

    @staticmethod
    def _user(ctx: RequestContext) -> Principal:
        principal = ctx.principal
        if principal is None or principal.kind not in (PrincipalKind.USER, PrincipalKind.SUBUSER):
            msg = "only users can link accounts of login providers"
            raise ForbiddenError(msg)
        return principal

    @staticmethod
    def _provider(name: str) -> str:
        if not PROVIDER_PATTERN.fullmatch(name):
            msg = "unknown login provider"
            raise NotFoundError(msg)
        return name

    # --- own links -------------------------------------------------------------------------------

    async def list_own(self, ctx: RequestContext) -> list[ExternalIdentity]:
        """The caller's linked provider accounts (also of providers turned off since)."""
        principal = self._user(ctx)
        async with self.deps.store.transaction() as tx:
            return await tx.external_identities.find(user_id=principal.subject, order_by="provider")

    async def start_link(
        self, ctx: RequestContext, provider: str, password: str | None
    ) -> RedirectStart:
        """Check the password again (or a recent step-up confirmation), then send the browser to the
        provider to pick the account that is linked to the caller."""
        principal = self._user(ctx)
        redirector = self.sessions.redirector(self._provider(provider))
        await self.reauth.check(ctx, principal, password, action="external_identity")
        async with self.deps.store.transaction() as tx:
            existing = await tx.external_identities.get(
                user_id=principal.subject, provider=provider
            )
        if existing is not None:
            msg = "an account of this provider is already linked; unlink it first"
            raise ConflictError(msg)
        return await redirector.start(RedirectPurpose.LINK, user_id=principal.subject)

    async def unlink(self, ctx: RequestContext, provider: str) -> None:
        """Remove the caller's link to ``provider`` (always allowed; the password stays)."""
        principal = self._user(ctx)
        name = self._provider(provider)
        async with self.deps.uow() as work:
            link = await work.tx.external_identities.get(user_id=principal.subject, provider=name)
            if link is None:
                msg = "no account of this provider is linked"
                raise NotFoundError(msg)
            await work.tx.external_identities.delete(id=link.id)
            await work.audit(
                ctx,
                "external_identity.unlinked",
                account_id=principal.account_id,
                target=("user", principal.subject),
                details={
                    "provider": link.provider,
                    "provider_subject": link.subject,
                    "display_name": link.display_name,
                },
            )

    # --- the provider's callback ---------------------------------------------------------------

    async def finish(
        self,
        provider: str,
        params: dict[str, str],
        browser_binding: str | None,
        ctx: RequestContext,
    ) -> RedirectOutcome:
        """The provider sent the browser back: finish the login or the link it was started for.

        Never raises for what the browser or the provider sent (404 only for a provider that is
        not enabled); the outcome says where the browser goes next. Every callback counts
        against the login limit of the address.
        """
        redirector = self.sessions.redirector(self._provider(provider))
        try:
            self.sessions.limit_login(ctx)
        except RateLimited:
            return RedirectOutcome(provider, RedirectPurpose.LOGIN, failure=RATE_LIMITED)
        callback = await redirector.callback(params, browser_binding)
        if callback.purpose is RedirectPurpose.LINK:
            return await self._finish_link(callback, ctx)
        try:
            pair = await self.sessions.login_redirect(callback, ctx)
        except AuthenticationFailed:
            failure = callback.failure or RedirectFailure.DENIED
            return RedirectOutcome(
                provider,
                RedirectPurpose.LOGIN,
                failure=failure.value,
                return_query=callback.return_query,
            )
        return RedirectOutcome(
            provider, RedirectPurpose.LOGIN, pair=pair, return_query=callback.return_query
        )

    async def _finish_link(
        self, callback: RedirectCallback, ctx: RequestContext
    ) -> RedirectOutcome:
        provider = callback.provider
        user_id, profile = callback.user_id, callback.profile
        if not callback.ok or user_id is None or profile is None:
            code = (callback.failure or RedirectFailure.DENIED).value
            await self._link_failed(ctx, provider, user_id, callback.reason or code)
            return RedirectOutcome(provider, RedirectPurpose.LINK, failure=code)
        now = self.deps.clock()
        failure: str | None = None
        reason = ""
        linked: ExternalIdentity | None = None
        async with self.deps.uow() as work:
            tx = work.tx
            found = await tx.users.get(id=user_id)
            taken = await tx.external_identities.get(provider=provider, subject=profile.subject)
            mine = await tx.external_identities.get(user_id=user_id, provider=provider)
            if found is None or found.status is not Status.ACTIVE:
                failure, reason = RedirectFailure.DENIED.value, "user disabled or removed"
            elif taken is not None and taken.user_id != user_id:
                failure, reason = TAKEN, "provider account linked to another user"
            elif mine is not None and mine.subject != profile.subject:
                failure, reason = ALREADY, "another account of this provider is linked"
            elif mine is not None:
                linked = mine  # linked again to the same account: nothing changes
            else:
                linked = ExternalIdentity(
                    id=uuid4(),
                    user_id=user_id,
                    provider=provider,
                    subject=profile.subject,
                    display_name=profile.display_name,
                    linked_at=now,
                )
                try:
                    await tx.external_identities.add(linked)
                except ConflictError:
                    failure, reason = TAKEN, "provider account linked meanwhile"
                else:
                    await self._audit_user(
                        tx,
                        ctx,
                        found,
                        "external_identity.linked",
                        {
                            "provider": provider,
                            "provider_subject": profile.subject,
                            "display_name": profile.display_name,
                        },
                    )
        if failure is not None or linked is None:
            await self._link_failed(ctx, provider, user_id, reason)
            failure = failure or RedirectFailure.DENIED.value
            return RedirectOutcome(provider, RedirectPurpose.LINK, failure=failure)
        return RedirectOutcome(provider, RedirectPurpose.LINK, linked=linked)

    async def _link_failed(
        self, ctx: RequestContext, provider: str, user_id: UUID | None, reason: str
    ) -> None:
        async with self.deps.store.transaction() as tx:
            found = await tx.users.get(id=user_id) if user_id else None
            details: dict[str, object] = {"provider": provider, "reason": reason}
            if found is None:
                entry = ctx.audit_entry(
                    at=self.deps.clock(),
                    action="external_identity.link_failed",
                    outcome="failure",
                    details=details,
                )
                await tx.audit.add(entry)
                return
            await self._audit_user(
                tx, ctx, found, "external_identity.link_failed", details, outcome="failure"
            )

    async def _audit_user(
        self,
        tx: Tx,
        ctx: RequestContext,
        found: User,
        action: str,
        details: dict[str, object],
        *,
        outcome: str = "success",
    ) -> None:
        """One audit row per account of the user, the user as the actor: the callback carries
        no token, the round trip was started by this user (password checked)."""
        memberships = await tx.memberships.find(user_id=found.id)
        accounts: list[UUID | None] = [m.account_id for m in memberships] or [None]
        for account_id in accounts:
            entry: AuditEntry = ctx.audit_entry(
                at=self.deps.clock(),
                action=action,
                outcome=outcome,
                account_id=account_id,
                target_type="user",
                target_id=str(found.id),
                details=details,
            )
            entry.actor_subject, entry.actor_kind, entry.actor_name = (
                f"user:{found.id}",
                "subuser" if found.kind is UserKind.SUBUSER else "user",
                found.display_name,
            )
            await tx.audit.add(entry)
