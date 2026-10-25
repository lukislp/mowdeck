"""Login, refresh with rotation and reuse detection, logout, API token exchange, me, password.

* Access tokens: EdDSA JWT, 10 minutes, claims from the database at issue time.
* Refresh tokens: opaque, stored as SHA-256, idle 7 days, absolute 30 days. Every refresh
  rotates; presenting a rotated token again revokes the whole login session (``family_id``)
  and is audited as ``session.refresh_reuse``. The JWT ``sid`` is the family id.
* Throttling: per client IP in memory (token bucket) and per username in the database (too
  many failures in the window answer 429 until the window has passed).
* Passkeys (ADR 0050): ``passkey_challenge`` and ``login_passkey`` run the passwordless login
  through the ``passkey`` authenticator plug-in and start the same session as a password login.
* Native app hand-off (ADR 0050): the app has a login run in the system browser. Whatever the
  method, the browser ends up with a fresh session and exchanges it (``issue_app_code``) for a
  one-time code bound to the app's PKCE challenge; ``redeem_app_code`` turns code and verifier
  into the app's own session.
"""

from dataclasses import dataclass, field
from datetime import datetime
from uuid import UUID, uuid4

from mowdeck_auth.issuer import IssuedToken, TokenIssuer
from mowdeck_contracts.v1 import Principal, PrincipalKind

from identity.application.access import token_principal, user_principal
from identity.application.authenticators import (
    AuthenticatorRegistry,
    AuthResult,
    Challenge,
    ChallengeAuthenticator,
)
from identity.application.context import RequestContext
from identity.application.deps import Deps
from identity.application.ports import Ge, Tx
from identity.application.ratelimit import TokenBucketLimiter
from identity.domain.credentials import (
    new_app_code,
    new_refresh_token,
    normalize_username,
    sha256_hex,
    validate_code_challenge,
    validate_password,
    verifier_matches,
)
from identity.domain.errors import AuthenticationFailed, ForbiddenError, NotFoundError, RateLimited
from identity.domain.models import (
    Account,
    AccountStatus,
    ApiToken,
    AppLoginCode,
    AppLoginRequest,
    LoginAttempt,
    Membership,
    Session,
    Status,
    User,
)


@dataclass(frozen=True, slots=True)
class TokenPair:
    """An access token and, for logins, the refresh token (shown once)."""

    access: IssuedToken
    principal: Principal
    refresh_token: str | None = None
    refresh_expires_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class MembershipView:
    """A membership as shown by ``GET /me``."""

    account_id: UUID
    account_name: str
    account_status: str
    is_owner: bool
    is_admin: bool
    status: str


@dataclass(frozen=True, slots=True)
class MeView:
    """``GET /me``: the principal, the user (or API token) and the memberships."""

    principal: Principal
    user: User | None = None
    token: ApiToken | None = None
    memberships: list[MembershipView] = field(default_factory=list)


def _usable_account(acct: Account | None, membership: Membership) -> bool:
    if acct is None or membership.status is not Status.ACTIVE:
        return False
    return acct.status is AccountStatus.ACTIVE or membership.is_owner


class SessionService:
    """Sessions and tokens."""

    def __init__(
        self,
        deps: Deps,
        issuer: TokenIssuer,
        authenticators: AuthenticatorRegistry,
        ip_limiter: TokenBucketLimiter,
        challenge_limiter: TokenBucketLimiter | None = None,
    ) -> None:
        self.deps = deps
        self.issuer = issuer
        self.authenticators = authenticators
        self.ip_limiter = ip_limiter
        self.challenge_limiter = challenge_limiter or TokenBucketLimiter(per_minute=60, capacity=30)

    # --- helpers -------------------------------------------------------------------------------

    async def _with_plan(self, principal: Principal, acct: Account) -> Principal:
        """The principal with the ``plan`` claim from Entitlements (ADR 0028).

        Without a client the stored ``accounts.plan`` stays. The client caches per account and
        answers ``unlimited`` while billing is disabled (also when Entitlements is down);
        with billing enabled and no answer the claim is empty.
        """
        plans = self.deps.entitlements
        if plans is None:
            return principal
        return principal.model_copy(update={"plan": await plans.plan_of(acct.id)})

    def _limit_ip(self, ctx: RequestContext, limiter: TokenBucketLimiter | None = None) -> None:
        wait = (limiter or self.ip_limiter).take(ctx.ip or "unknown")
        if wait > 0:
            msg = "too many attempts from this address; try again later"
            raise RateLimited(msg, int(wait) + 1)

    async def _record_failure(
        self, ctx: RequestContext, username_norm: str | None, reason: str, subject: str | None
    ) -> None:
        now = self.deps.clock()
        async with self.deps.store.transaction() as tx:
            if username_norm:
                await tx.login_attempts.add(
                    LoginAttempt(uuid4(), now, username_norm, False, ctx.ip, reason)
                )
            # Failures of a known user or token also land in the audit log of its accounts.
            accounts: list[UUID | None] = [None]
            if subject and username_norm:
                memberships = await tx.memberships.find(user_id=UUID(subject))
                accounts = [m.account_id for m in memberships] or [None]
            elif subject:
                token = await tx.api_tokens.get(id=UUID(subject))
                accounts = [token.account_id if token else None]
            for account_id in accounts:
                entry = ctx.audit_entry(
                    at=now,
                    action="session.login_failed" if username_norm else "api_token.exchange_failed",
                    outcome="failure",
                    account_id=account_id,
                    details={"username": username_norm, "reason": reason}
                    if username_norm
                    else {"reason": reason},
                    target_type="user"
                    if subject and username_norm
                    else ("api_token" if subject else None),
                    target_id=subject,
                )
                await tx.audit.add(entry)

    async def _pick_membership(
        self, tx: Tx, found: User, account_id: UUID | None
    ) -> tuple[Membership, Account]:
        candidates: list[tuple[Membership, Account]] = []
        for membership in await tx.memberships.find(user_id=found.id, order_by="created_at"):
            acct = await tx.accounts.get(id=membership.account_id)
            if acct is not None and _usable_account(acct, membership):
                candidates.append((membership, acct))
        if account_id is not None:
            candidates = [c for c in candidates if c[1].id == account_id]
        if not candidates:
            raise AuthenticationFailed("no usable membership")

        def rank(item: tuple[Membership, Account]) -> tuple[int, int]:
            membership, acct = item
            return (0 if acct.id == found.owner_account_id else 1, 0 if membership.is_owner else 1)

        return sorted(candidates, key=rank)[0]

    # --- login, refresh, logout ----------------------------------------------------------------

    async def login(
        self, username: str, password: str, account_id: UUID | None, ctx: RequestContext
    ) -> TokenPair:
        """Password login; returns an access and a refresh token."""
        self._limit_ip(ctx)
        norm = normalize_username(username)
        now = self.deps.clock()
        config = self.deps.config
        async with self.deps.store.transaction() as tx:
            failures = await tx.login_attempts.count(
                username_norm=norm, success=False, at=Ge(now - config.login_failure_window)
            )
        if failures >= config.login_max_failures:
            async with self.deps.uow() as work:
                await work.audit(
                    ctx, "session.login_throttled", outcome="denied", details={"username": norm}
                )
            msg = "too many failed logins for this user; try again later"
            raise RateLimited(msg, int(config.login_failure_window.total_seconds()))
        authenticator = self.authenticators.get("password")
        if authenticator is None:
            msg = "password login is disabled on this installation"
            raise ForbiddenError(msg)
        result = await authenticator.authenticate({"username": username, "password": password})
        if not result.ok or result.subject_id is None:
            subject = str(result.subject_id) if result.subject_id else None
            await self._record_failure(ctx, norm, result.reason or "failed", subject)
            raise AuthenticationFailed(result.reason or "failed")
        try:
            pair = await self._start_session(result.subject_id, account_id, ctx)
        except AuthenticationFailed as exc:
            await self._record_failure(ctx, norm, exc.reason, str(result.subject_id))
            raise
        if result.needs_rehash:
            await self._rehash(result.subject_id, password)
        return pair

    async def _start_session(
        self,
        user_id: UUID,
        account_id: UUID | None,
        ctx: RequestContext,
        *,
        method: str = "password",
        details: dict[str, object] | None = None,
    ) -> TokenPair:
        now = self.deps.clock()
        config = self.deps.config
        refresh = new_refresh_token()
        async with self.deps.uow() as work:
            tx = work.tx
            found = await tx.users.get(id=user_id)
            if found is None:
                raise AuthenticationFailed("user vanished")
            membership, acct = await self._pick_membership(tx, found, account_id)
            session = Session(
                id=uuid4(),
                family_id=uuid4(),
                user_id=found.id,
                account_id=acct.id,
                refresh_hash=sha256_hex(refresh),
                created_at=now,
                idle_expires_at=now + config.refresh_idle,
                absolute_expires_at=now + config.refresh_absolute,
                last_used_at=now,
                user_agent=(ctx.user_agent or "")[:200] or None,
                ip=ctx.ip,
            )
            await tx.sessions.add(session)
            await tx.login_attempts.add(
                LoginAttempt(uuid4(), now, found.username_norm, True, ctx.ip)
            )
            found.last_login_at = now
            await tx.users.update(found)
            catalogue = await self.deps.catalogue.current(tx)
            principal = await user_principal(
                tx, found, acct, membership, catalogue, session_id=session.family_id
            )
            principal = await self._with_plan(principal, acct)
            access = self.issuer.issue(principal)
            entry = ctx.audit_entry(
                at=now,
                action="session.login",
                account_id=acct.id,
                target_type="user",
                target_id=str(found.id),
                details={
                    "method": method,
                    "session": str(session.family_id),
                    "jti": str(access.token_id),
                    **(details or {}),
                },
            )
            entry.actor_subject, entry.actor_kind, entry.actor_name = (
                f"user:{found.id}",
                principal.kind.value,
                found.display_name,
            )
            await tx.audit.add(entry)
            if method == "passkey" and details and "via" not in details:
                used = ctx.audit_entry(
                    at=now,
                    action="passkey.used",
                    account_id=acct.id,
                    target_type="user",
                    target_id=str(found.id),
                    details={"passkey": details.get("passkey"), "session": str(session.family_id)},
                )
                used.actor_subject, used.actor_kind, used.actor_name = (
                    entry.actor_subject,
                    entry.actor_kind,
                    entry.actor_name,
                )
                await tx.audit.add(used)
        return TokenPair(access, principal, refresh, session.idle_expires_at)

    # --- passkeys (ADR 0050) --------------------------------------------------------------------

    def _passkeys(self) -> ChallengeAuthenticator:
        authenticator = self.authenticators.challenger("passkey")
        if authenticator is None:
            msg = "passkeys are not enabled on this installation"
            raise ForbiddenError(msg)
        return authenticator

    async def passkey_challenge(self, ctx: RequestContext) -> Challenge:
        """Options for a passwordless login. A challenge checks nothing and every sign-in page
        asks for one (the autofill offer), so it has the looser limit of refreshes; answering it
        has the login limit."""
        self._limit_ip(ctx, self.challenge_limiter)
        return await self._passkeys().challenge()

    async def login_passkey(
        self, ceremony_id: UUID, credential: str, account_id: UUID | None, ctx: RequestContext
    ) -> TokenPair:
        """Passwordless login with a passkey: the same session as a password login."""
        self._limit_ip(ctx)
        result = await self._passkeys().authenticate(
            {"ceremony_id": str(ceremony_id), "credential": credential}
        )
        if not result.ok or result.subject_id is None:
            await self._record_passkey_failure(ctx, result)
            raise AuthenticationFailed(result.reason or "failed")
        try:
            return await self._start_session(
                result.subject_id,
                account_id,
                ctx,
                method="passkey",
                details={"passkey": result.credential_ref},
            )
        except AuthenticationFailed as exc:
            failed = AuthResult(
                False,
                result.subject_id,
                "passkey",
                exc.reason,
                credential_ref=result.credential_ref,
            )
            await self._record_passkey_failure(ctx, failed)
            raise

    # --- native app hand-off (ADR 0050) -----------------------------------------------------------

    async def start_app_login(self, code_challenge: str, ctx: RequestContext) -> AppLoginRequest:
        """The native app's request to sign in through the system browser: the S256 hash of its
        PKCE verifier is stored, the browser page gets only the request id. Rate limited like a
        passkey challenge (it checks nothing)."""
        self._limit_ip(ctx, self.challenge_limiter)
        validate_code_challenge(code_challenge)
        now = self.deps.clock()
        request = AppLoginRequest(
            id=uuid4(),
            code_challenge=code_challenge,
            created_at=now,
            expires_at=now + self.deps.config.app_request_ttl,
        )
        async with self.deps.store.transaction() as tx:
            await tx.app_login_requests.add(request)
        return request

    async def issue_app_code(self, ctx: RequestContext, request_id: UUID) -> str:
        """Hand the caller's fresh browser login to the native app.

        Login-method agnostic: whatever started the session in the system browser (password,
        passkey, a redirect provider) ends here. Only for a request the app started (single
        use, ``app_request_ttl``); the session must be younger than ``app_handoff_login_age``
        (a session left in the browser is never handed over silently); it is ended in the same
        transaction, and the one-time code is bound to the request's PKCE challenge.
        """
        principal = self._user_principal(ctx)
        now = self.deps.clock()
        async with self.deps.store.transaction() as tx:
            request = await tx.app_login_requests.get(id=request_id)
            taken = await tx.app_login_requests.delete(id=request_id) if request else 0
        if request is None or taken != 1 or now >= request.expires_at:
            msg = "this sign-in request of the app is unknown or expired; start again in the app"
            raise ForbiddenError(msg)
        code_challenge = request.code_challenge
        if principal.session_id is None or principal.account_id is None:
            msg = "only a login session can be handed to the app"
            raise ForbiddenError(msg)
        code = new_app_code()
        async with self.deps.uow() as work:
            tx = work.tx
            family = await tx.sessions.find(
                family_id=principal.session_id, order_by="created_at", limit=1
            )
            live = await tx.sessions.count(family_id=principal.session_id, revoked_at=None)
            fresh = (
                bool(family)
                and live > 0
                and now - family[0].created_at <= self.deps.config.app_handoff_login_age
            )
            if not fresh:
                await work.audit(
                    ctx,
                    "app_login.denied",
                    account_id=principal.account_id,
                    target=("user", principal.subject),
                    details={"session": str(principal.session_id), "reason": "login not fresh"},
                    outcome="denied",
                )
            else:
                await self._revoke_family(tx, principal.session_id, "handed to the app")
                await tx.app_login_codes.add(
                    AppLoginCode(
                        id=uuid4(),
                        code_hash=sha256_hex(code),
                        code_challenge=code_challenge,
                        user_id=principal.subject,
                        account_id=principal.account_id,
                        created_at=now,
                        expires_at=now + self.deps.config.app_code_ttl,
                        source_session=principal.session_id,
                    )
                )
                await work.audit(
                    ctx,
                    "app_login.code_issued",
                    account_id=principal.account_id,
                    target=("user", principal.subject),
                    details={"session": str(principal.session_id)},
                )
        if not fresh:
            msg = "log in again to hand the login to the app"
            raise ForbiddenError(msg)
        return code

    async def redeem_app_code(self, code: str, verifier: str, ctx: RequestContext) -> TokenPair:
        """The native app's half of the hand-off: the one-time code and the PKCE verifier of the
        challenge it was issued for give a normal session (as a password login does)."""
        self._limit_ip(ctx)
        now = self.deps.clock()
        async with self.deps.store.transaction() as tx:
            record = await tx.app_login_codes.get(code_hash=sha256_hex(code))
            taken = await tx.app_login_codes.delete(id=record.id) if record else 0
            found = await tx.users.get(id=record.user_id) if record else None
        reason = None
        if record is None or taken != 1:
            reason = "unknown app code"
        elif now >= record.expires_at:
            reason = "expired app code"
        elif not verifier_matches(record.code_challenge, verifier):
            reason = "app code verifier mismatch"
        elif found is None or found.status is not Status.ACTIVE:
            reason = "user disabled"
        if reason is not None or record is None:
            await self._record_handoff_failure(ctx, record, reason or "failed")
            raise AuthenticationFailed(reason or "failed")
        details: dict[str, object] = {"via": "app", "from_session": str(record.source_session)}
        return await self._start_session(
            record.user_id, record.account_id, ctx, method="app_handoff", details=details
        )

    async def _record_handoff_failure(
        self, ctx: RequestContext, record: AppLoginCode | None, reason: str
    ) -> None:
        now = self.deps.clock()
        async with self.deps.store.transaction() as tx:
            entry = ctx.audit_entry(
                at=now,
                action="app_login.failed",
                outcome="failure",
                account_id=record.account_id if record else None,
                details={"reason": reason},
                target_type="user" if record else None,
                target_id=str(record.user_id) if record else None,
            )
            await tx.audit.add(entry)

    async def _record_passkey_failure(self, ctx: RequestContext, result: AuthResult) -> None:
        """Audit a failed passkey login. No ``login_attempts`` row: a passkey has no secret to
        guess, and failures must not lock the user's password login (the IP limit applies)."""
        now = self.deps.clock()
        details: dict[str, object] = {"method": "passkey", "reason": result.reason or "failed"}
        if result.credential_ref:
            details["passkey"] = result.credential_ref
        async with self.deps.store.transaction() as tx:
            accounts: list[UUID | None] = [None]
            if result.subject_id is not None:
                memberships = await tx.memberships.find(user_id=result.subject_id)
                accounts = [m.account_id for m in memberships] or [None]
            for account in accounts:
                entry = ctx.audit_entry(
                    at=now,
                    action="session.login_failed",
                    outcome="failure",
                    account_id=account,
                    details=details,
                    target_type="user" if result.subject_id else None,
                    target_id=str(result.subject_id) if result.subject_id else None,
                )
                await tx.audit.add(entry)

    async def _rehash(self, user_id: UUID, password: str) -> None:
        new_hash = await self.deps.hasher.hash(password)
        async with self.deps.store.transaction() as tx:
            found = await tx.users.get(id=user_id)
            if found is not None:
                found.password_hash = new_hash
                await tx.users.update(found)

    async def _revoke_family(self, tx: Tx, family_id: UUID, reason: str) -> int:
        now = self.deps.clock()
        count = 0
        for session in await tx.sessions.find(family_id=family_id, revoked_at=None):
            session.revoked_at, session.revoke_reason = now, reason
            await tx.sessions.update(session)
            count += 1
        return count

    async def refresh(self, refresh_token: str, ctx: RequestContext) -> TokenPair:
        """Rotate the refresh token and issue a new access token with fresh claims.

        A rotated token presented again normally revokes the whole login session. Within
        ``refresh_reuse_grace`` after its rotation, and only while its successor was never used,
        it is treated as a lost response instead: the unused successor is rotated in its place
        (audited as ``session.refresh_grace``), so a dropped mobile connection does not log the
        user out.
        """
        now = self.deps.clock()
        async with self.deps.store.transaction() as tx:
            session = await tx.sessions.get(refresh_hash=sha256_hex(refresh_token))
            successor = None
            if session is not None and session.replaced_by is not None:
                successor = await tx.sessions.get(id=session.replaced_by)
        if session is None:
            raise AuthenticationFailed("unknown refresh token")
        if session.replaced_by is not None:
            if self._within_grace(session, successor, now):
                assert successor is not None  # noqa: S101 - checked by _within_grace
                return await self._rotate(successor, ctx, grace_for=session)
            async with self.deps.uow() as work:
                await self._revoke_family(work.tx, session.family_id, "refresh token reuse")
                await work.audit(
                    ctx,
                    "session.refresh_reuse",
                    account_id=session.account_id,
                    target=("user", session.user_id),
                    details={"session": str(session.family_id)},
                    outcome="denied",
                )
            raise AuthenticationFailed("refresh token reuse")
        return await self._rotate(session, ctx)

    def _within_grace(self, session: Session, successor: Session | None, now: datetime) -> bool:
        grace = self.deps.config.refresh_reuse_grace
        return (
            grace.total_seconds() > 0
            and session.last_used_at is not None
            and now - session.last_used_at <= grace
            and successor is not None
            and successor.replaced_by is None
            and successor.revoked_at is None
        )

    async def _rotate(
        self, session: Session, ctx: RequestContext, *, grace_for: Session | None = None
    ) -> TokenPair:
        """Replace ``session`` by a successor and issue an access token for it."""
        now = self.deps.clock()
        config = self.deps.config
        if session.revoked_at is not None:
            raise AuthenticationFailed("session revoked")
        if now >= session.idle_expires_at or now >= session.absolute_expires_at:
            raise AuthenticationFailed("session expired")
        new_token = new_refresh_token()
        async with self.deps.uow() as work:
            tx = work.tx
            found = await tx.users.get(id=session.user_id)
            membership = await tx.memberships.get(
                account_id=session.account_id, user_id=session.user_id
            )
            acct = await tx.accounts.get(id=session.account_id)
            usable = (
                found is not None
                and found.status is Status.ACTIVE
                and membership is not None
                and _usable_account(acct, membership)
            )
            if not usable or found is None or membership is None or acct is None:
                await self._revoke_family(tx, session.family_id, "user or account no longer usable")
                failed = True
            else:
                failed = False
                successor = Session(
                    id=uuid4(),
                    family_id=session.family_id,
                    user_id=session.user_id,
                    account_id=session.account_id,
                    refresh_hash=sha256_hex(new_token),
                    created_at=now,
                    idle_expires_at=min(now + config.refresh_idle, session.absolute_expires_at),
                    absolute_expires_at=session.absolute_expires_at,
                    last_used_at=now,
                    user_agent=(ctx.user_agent or "")[:200] or None,
                    ip=ctx.ip,
                )
                await tx.sessions.add(successor)
                session.replaced_by, session.last_used_at = successor.id, now
                await tx.sessions.update(session)
                catalogue = await self.deps.catalogue.current(tx)
                principal = await user_principal(
                    tx, found, acct, membership, catalogue, session_id=session.family_id
                )
                principal = await self._with_plan(principal, acct)
                access = self.issuer.issue(principal)
                details: dict[str, object] = {
                    "session": str(session.family_id),
                    "jti": str(access.token_id),
                }
                if grace_for is not None:
                    details["replaced_unused"] = str(session.id)
                await work.audit(
                    ctx,
                    "session.refresh_grace" if grace_for is not None else "session.refreshed",
                    account_id=acct.id,
                    target=("user", found.id),
                    details=details,
                )
        if failed:
            raise AuthenticationFailed("user or account no longer usable")
        return TokenPair(access, principal, new_token, successor.idle_expires_at)

    async def logout(self, refresh_token: str, ctx: RequestContext) -> None:
        """End the login session of ``refresh_token`` (unknown tokens are ignored)."""
        async with self.deps.uow() as work:
            session = await work.tx.sessions.get(refresh_hash=sha256_hex(refresh_token))
            if session is None:
                return
            await self._revoke_family(work.tx, session.family_id, "logout")
            await work.audit(
                ctx,
                "session.logout",
                account_id=session.account_id,
                target=("user", session.user_id),
                details={"session": str(session.family_id)},
            )

    async def logout_all(self, ctx: RequestContext) -> int:
        """End every session of the calling user."""
        principal = self._user_principal(ctx)
        now = self.deps.clock()
        async with self.deps.uow() as work:
            sessions = await work.tx.sessions.find(user_id=principal.subject, revoked_at=None)
            for session in sessions:
                session.revoked_at, session.revoke_reason = now, "logout all"
                await work.tx.sessions.update(session)
            await work.audit(
                ctx,
                "session.logout_all",
                account_id=principal.account_id,
                target=("user", principal.subject),
                details={"sessions": len(sessions)},
            )
        return len(sessions)

    @staticmethod
    def _user_principal(ctx: RequestContext) -> Principal:
        principal = ctx.principal
        if principal is None or principal.kind not in (PrincipalKind.USER, PrincipalKind.SUBUSER):
            msg = "only users have sessions"
            raise ForbiddenError(msg)
        return principal

    # --- API tokens -----------------------------------------------------------------------------

    async def exchange(self, token: str, ctx: RequestContext) -> TokenPair:
        """Exchange an API token for a short access token (``kind=api_token``)."""
        self._limit_ip(ctx)
        authenticator = self.authenticators.get("api_token")
        if authenticator is None:
            msg = "API tokens are disabled on this installation"
            raise ForbiddenError(msg)
        result = await authenticator.authenticate({"token": token})
        if not result.ok or result.subject_id is None:
            subject = str(result.subject_id) if result.subject_id else None
            await self._record_failure(ctx, None, result.reason or "failed", subject)
            raise AuthenticationFailed(result.reason or "failed")
        now = self.deps.clock()
        async with self.deps.uow() as work:
            tx = work.tx
            api = await tx.api_tokens.get(id=result.subject_id)
            acct = await tx.accounts.get(id=api.account_id) if api else None
            if api is None or acct is None:
                raise AuthenticationFailed("token vanished")
            if (
                api.last_used_at is None
                or now - api.last_used_at >= self.deps.config.api_token_touch_interval
            ):
                api.last_used_at = now
                await tx.api_tokens.update(api)
            principal = token_principal(api, acct, await self.deps.catalogue.current(tx))
            principal = await self._with_plan(principal, acct)
            access = self.issuer.issue(principal)
            entry = ctx.audit_entry(
                at=now,
                action="api_token.exchanged",
                account_id=acct.id,
                target_type="api_token",
                target_id=str(api.id),
                details={"jti": str(access.token_id)},
            )
            entry.actor_subject, entry.actor_kind, entry.actor_name = (
                f"api_token:{api.id}",
                "api_token",
                api.name,
            )
            await tx.audit.add(entry)
        return TokenPair(access, principal)

    # --- me and password ------------------------------------------------------------------------

    async def me(self, principal: Principal) -> MeView:
        """The caller: user or API token, and the memberships of a user."""
        async with self.deps.store.transaction() as tx:
            if principal.kind is PrincipalKind.API_TOKEN:
                return MeView(principal, token=await tx.api_tokens.get(id=principal.subject))
            found = await tx.users.get(id=principal.subject)
            if found is None:
                msg = "user not found"
                raise NotFoundError(msg)
            views = []
            for membership in await tx.memberships.find(user_id=found.id, order_by="created_at"):
                acct = await tx.accounts.get(id=membership.account_id)
                if acct is None:
                    continue
                views.append(
                    MembershipView(
                        acct.id,
                        acct.name,
                        acct.status.value,
                        membership.is_owner,
                        membership.is_admin,
                        membership.status.value,
                    )
                )
        return MeView(principal, user=found, memberships=views)

    async def change_password(self, ctx: RequestContext, current: str, new: str) -> None:
        """Change the caller's password; other sessions of the caller end."""
        principal = self._user_principal(ctx)
        async with self.deps.store.transaction() as tx:
            found = await tx.users.get(id=principal.subject)
        if found is None or found.password_hash is None:
            raise AuthenticationFailed("no password set")
        if not await self.deps.hasher.verify(found.password_hash, current):
            await self._record_failure(
                ctx, found.username_norm, "wrong current password", str(found.id)
            )
            raise AuthenticationFailed("wrong current password")
        validate_password(new, username=found.username)
        new_hash = await self.deps.hasher.hash(new)
        now = self.deps.clock()
        async with self.deps.uow() as work:
            fresh = await work.tx.users.get(id=found.id)
            if fresh is None:
                raise AuthenticationFailed("user vanished")
            fresh.password_hash, fresh.must_change_password = new_hash, False
            await work.tx.users.update(fresh)
            ended = 0
            for session in await work.tx.sessions.find(user_id=found.id, revoked_at=None):
                if session.family_id != principal.session_id:
                    session.revoked_at, session.revoke_reason = now, "password changed"
                    await work.tx.sessions.update(session)
                    ended += 1
            await work.audit(
                ctx,
                "user.password_changed",
                account_id=principal.account_id,
                target=("user", found.id),
                details={"sessions_ended": ended},
            )
