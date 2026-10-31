"""Re-authentication before a sensitive change of the own login methods.

Adding a passkey (ADR 0050) and linking a GitHub account (ADR 0051) both need the current
password again, right before the change. The check behaves like a password login of the user:

* the login limit per client IP applies (in-process; Traefik's ``identity-auth`` in front),
* the per-username failure throttle applies (429 after too many failures in the window),
* a wrong password counts as a failed login of the user (``login_attempts``) and is audited as
  ``<action>.reauth_failed``.
"""

from uuid import uuid4

from mowdeck_auth.step_up import ensure_recent_auth
from mowdeck_contracts.v1 import Principal
from mowdeck_service_kit.client_address import limiter_key

from identity.application.context import RequestContext
from identity.application.deps import Deps
from identity.application.ports import Ge
from identity.application.ratelimit import TokenBucketLimiter
from identity.domain.errors import AuthenticationFailed, RateLimited
from identity.domain.models import LoginAttempt, User


class Reauthenticator:
    """Checks the caller's current password before a change of their login methods."""

    def __init__(self, deps: Deps, ip_limiter: TokenBucketLimiter | None = None) -> None:
        self.deps = deps
        self.ip_limiter = ip_limiter

    async def check(
        self, ctx: RequestContext, principal: Principal, password: str | None, *, action: str
    ) -> User:
        """The caller (a user) after the password matched; ``action`` names the audit prefix
        (``passkey``, ``external_identity``). Raises ``RateLimited`` or
        ``AuthenticationFailed``.

        Without a password the caller must have confirmed recently instead (step-up, ADR 0057:
        ``ReauthRequired`` otherwise), so one sheet in the web app replaces the password field.
        """
        if password is None:
            ensure_recent_auth(principal, now=self.deps.clock())
            async with self.deps.store.transaction() as tx:
                found = await tx.users.get(id=principal.subject)
            if found is None:
                raise AuthenticationFailed("user vanished")
            return found
        if self.ip_limiter is not None:
            wait = self.ip_limiter.take(limiter_key(ctx.ip))
            if wait > 0:
                msg = "too many attempts from this address; try again later"
                raise RateLimited(msg, int(wait) + 1)
        async with self.deps.store.transaction() as tx:
            found = await tx.users.get(id=principal.subject)
        if found is None or found.password_hash is None:
            raise AuthenticationFailed("no password set")
        await self._check_throttle(found)
        if not await self.deps.hasher.verify(found.password_hash, password):
            await self._failed(ctx, found, principal, action)
            raise AuthenticationFailed(f"wrong password for {action.replace('_', ' ')} change")
        return found

    async def _check_throttle(self, found: User) -> None:
        """The username throttle of the password login applies to the re-authentication too."""
        config = self.deps.config
        since = self.deps.clock() - config.login_failure_window
        async with self.deps.store.transaction() as tx:
            failures = await tx.login_attempts.count(
                username_norm=found.username_norm, success=False, at=Ge(since)
            )
        if failures >= config.login_max_failures:
            msg = "too many failed attempts for this user; try again later"
            raise RateLimited(msg, int(config.login_failure_window.total_seconds()))

    async def _failed(
        self, ctx: RequestContext, found: User, principal: Principal, action: str
    ) -> None:
        """A wrong password counts like a failed login of the user."""
        now = self.deps.clock()
        reason = f"{action.replace('_', ' ')} reauth"
        async with self.deps.uow() as work:
            await work.tx.login_attempts.add(
                LoginAttempt(uuid4(), now, found.username_norm, False, ctx.ip, reason)
            )
            await work.audit(
                ctx,
                f"{action}.reauth_failed",
                account_id=principal.account_id,
                target=("user", found.id),
                outcome="failure",
            )
