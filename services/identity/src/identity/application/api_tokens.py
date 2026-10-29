"""API tokens: long-lived credentials with explicit grants, capped at the creator's rights.

A token is its own subject (``api_token:<id>``) with ``member`` on its account and ``grant_*``
tuples for its grants. It never inherits the creator's rights, can never hold administrative
keys (``users.manage``, ``billing.manage``, ``audit.view``), and is revoked when its creator is
disabled or removed. The secret is shown once; only its SHA-256 is stored.

Personal access tokens (ADR 0064) are API tokens with ``purpose=personal`` that any user creates
for their own scripts: only the curated :data:`PERSONAL_SCOPES`, always with an expiry of at most
:data:`PERSONAL_MAX_LIFETIME`, created only after a recent step-up (the route requires it) and
managed by their creator through ``/me/api-tokens``. Like every token they appear in the
account's token list, so an admin sees and can revoke them.

Revoked tokens stay listed until the owner deletes them for good (``purge`` for one,
``purge_revoked`` for all of the account). Only revoked tokens can be deleted: an active token
is revoked first, so its ``api_token.revoked`` event always goes out. The audit rows of a deleted
token stay (``audit_log`` is append-only and refers to the token by id only, no foreign key).
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from uuid import UUID, uuid4

from mowdeck_authz import api_token
from mowdeck_contracts.v1 import (
    IDENTITY_API_TOKEN_REVOKED,
    ApiTokenRevoked,
    Principal,
    PrincipalKind,
)

from identity.application.context import RequestContext
from identity.application.deps import Deps
from identity.application.ports import In
from identity.application.work import Work
from identity.domain.credentials import new_api_token, sha256_hex
from identity.domain.errors import ConflictError, ForbiddenError, InvalidInput, NotFoundError
from identity.domain.models import ApiToken, Level, LevelType, TokenGrant, TokenPurpose
from identity.domain.permissions import validate_token_grant

WIDGET_PREFIX = "iOS widgets: "
"""Name prefix that marks an API token as an iOS widget credential (ADR 0059). The token shows
up in the account's token list like any other, so an admin can see and revoke it."""

WIDGET_KEYS: tuple[str, ...] = ("mower.view", "mower.control")
"""The only keys a widget credential can hold, on the account level: see the mowers and start,
pause and dock them. A key the creator does not hold is left out."""

WIDGET_LIFETIME = timedelta(days=90)
"""A widget credential expires after this long; the app replaces it well before."""

MAX_WIDGET_CREDENTIALS = 10
"""Active widget credentials one user can hold (one per iPhone or iPad)."""


PERSONAL_SCOPES: tuple[str, ...] = ("mower.view", "mower.control", "schedule.edit")
"""The keys a personal access token can hold: see the mowers (state, telemetry, history,
realtime through the implied account keys), start, pause and dock them, edit schedules. Nothing
that drives a mower by hand, changes devices or firmware, or manages users, tokens or billing."""

PERSONAL_VIEW_KEY = "mower.view"
"""Every other personal scope needs it (as in roles: control without seeing makes no sense)."""

PERSONAL_MAX_LIFETIME_DAYS = 365
"""A personal access token expires after at most this many days; there is no token for ever."""

MAX_PERSONAL_TOKENS = 25
"""Active personal access tokens one user can hold."""


def is_widget_token(token: ApiToken) -> bool:
    """True for tokens created by :meth:`ApiTokenService.create_widget_credential`."""
    return token.name.startswith(WIDGET_PREFIX)


@dataclass(frozen=True, slots=True)
class CreatedToken:
    """A new token and its secret (returned once)."""

    token: ApiToken
    secret: str


class ApiTokenService:
    """Use cases around API tokens."""

    def __init__(self, deps: Deps) -> None:
        self.deps = deps

    async def list_tokens(self, account_id: UUID) -> list[ApiToken]:
        """Tokens of the account, newest first (secrets are never returned)."""
        async with self.deps.store.transaction() as tx:
            return await tx.api_tokens.find(
                account_id=account_id, order_by="created_at", descending=True
            )

    async def create(
        self,
        account_id: UUID,
        name: str,
        grants: Sequence[TokenGrant],
        expires_at: datetime | None,
        ctx: RequestContext,
    ) -> CreatedToken:
        """Create a token; every grant must be held by the creator on its level."""
        actor = ctx.principal
        if actor is None or actor.kind not in (PrincipalKind.USER, PrincipalKind.SUBUSER):
            msg = "only users can create API tokens"
            raise ForbiddenError(msg)
        cleaned = name.strip()
        if not 1 <= len(cleaned) <= 100:
            msg = "token names have 1 to 100 characters"
            raise InvalidInput(msg)
        if not grants:
            msg = "a token needs at least one grant"
            raise InvalidInput(msg)
        now = self.deps.clock()
        if expires_at is not None and expires_at <= now:
            msg = "expires_at must be in the future"
            raise InvalidInput(msg)
        token_id = uuid4()
        secret_token, secret = new_api_token(token_id)
        async with self.deps.uow() as work:
            work.account_id = account_id
            tx = work.tx
            catalogue = await self.deps.catalogue.current(tx)
            checked: list[TokenGrant] = []
            for grant in grants:
                keys = validate_token_grant(grant.permissions, grant.level.type, catalogue)
                await self.deps.access.require_level_in_account(grant.level, account_id)
                await self.deps.access.require_holds(
                    tx, actor, account_id, keys, grant.level, catalogue
                )
                checked.append(TokenGrant(grant.level, tuple(keys)))
            token = ApiToken(
                id=token_id,
                account_id=account_id,
                name=cleaned,
                prefix=secret_token[:12],
                secret_hash=sha256_hex(secret),
                created_by=actor.subject,
                grants=[g.to_json() for g in checked],
                created_at=now,
                expires_at=expires_at,
            )
            await tx.api_tokens.add(token)
            work.touch(api_token(token.id))
            await work.audit(
                ctx,
                "api_token.created",
                account_id=account_id,
                target=("api_token", token.id),
                details={
                    "name": cleaned,
                    "grants": token.grants,
                    "expires_at": expires_at.isoformat() if expires_at else None,
                },
            )
        return CreatedToken(token, secret_token)

    async def revoke(self, account_id: UUID, token_id: UUID, ctx: RequestContext) -> ApiToken:
        """Revoke a token of the account (404 for tokens of other accounts)."""
        now = self.deps.clock()
        async with self.deps.uow() as work:
            work.account_id = account_id
            token = await work.tx.api_tokens.get(id=token_id, account_id=account_id)
            if token is None:
                msg = "API token not found"
                raise NotFoundError(msg)
            if token.revoked_at is None:
                token.revoked_at, token.revoke_reason = now, "revoked"
                await work.tx.api_tokens.update(token)
                work.touch(api_token(token.id))
                work.emit(
                    IDENTITY_API_TOKEN_REVOKED,
                    account_id,
                    ApiTokenRevoked(token_id=token.id, account_id=account_id, reason="revoked"),
                )
                await work.audit(
                    ctx,
                    "api_token.revoked",
                    account_id=account_id,
                    target=("api_token", token.id),
                    details={"name": token.name},
                )
        return token

    async def list_widget_credentials(self, account_id: UUID, user_id: UUID) -> list[ApiToken]:
        """The caller's own widget credentials (active and revoked), newest first."""
        found = await self.list_tokens(account_id)
        return [t for t in found if is_widget_token(t) and t.created_by == user_id]

    async def create_widget_credential(
        self,
        account_id: UUID,
        device_name: str,
        replaces: UUID | None,
        ctx: RequestContext,
    ) -> CreatedToken:
        """Create the credential the iOS widgets use (ADR 0059): an API token with
        ``mower.view`` and ``mower.control`` on the account (as far as the caller holds them),
        valid for :data:`WIDGET_LIFETIME`, named ``iOS widgets: <device>``.

        Any user can create one for themselves; it never exceeds the creator's rights and is
        revoked with the creator. ``replaces`` (an earlier widget credential of the caller)
        is revoked in the same transaction, which is how the app rotates the credential.
        """
        actor = ctx.principal
        if actor is None or actor.kind not in (PrincipalKind.USER, PrincipalKind.SUBUSER):
            msg = "only users can create widget credentials"
            raise ForbiddenError(msg)
        label = " ".join(device_name.split())[:80]
        if not label:
            msg = "the device needs a name"
            raise InvalidInput(msg)
        now = self.deps.clock()
        token_id = uuid4()
        secret_token, secret = new_api_token(token_id)
        async with self.deps.uow() as work:
            work.account_id = account_id
            tx = work.tx
            existing = [
                t
                for t in await tx.api_tokens.find(account_id=account_id)
                if is_widget_token(t) and t.created_by == actor.subject
            ]
            if replaces is not None:
                old = next((t for t in existing if t.id == replaces), None)
                if old is None:
                    msg = "widget credential not found"
                    raise NotFoundError(msg)
                await self._revoke_in(work, old, account_id, ctx)
            active = [t for t in existing if t.is_usable(now) and t.id != replaces]
            if len(active) >= MAX_WIDGET_CREDENTIALS:
                msg = "too many active widget credentials, revoke one first"
                raise ConflictError(msg)
            catalogue = await self.deps.catalogue.current(tx)
            level = Level(LevelType.ACCOUNT, account_id)
            held: list[str] = []
            for key in WIDGET_KEYS:
                try:
                    await self.deps.access.require_holds(
                        tx, actor, account_id, [key], level, catalogue
                    )
                except ForbiddenError:
                    continue
                held.append(key)
            if "mower.view" not in held:
                msg = "widgets need the right to see all of the account's mowers"
                raise ForbiddenError(msg)
            grant = TokenGrant(level, tuple(validate_token_grant(held, level.type, catalogue)))
            token = ApiToken(
                id=token_id,
                account_id=account_id,
                name=f"{WIDGET_PREFIX}{label}",
                prefix=secret_token[:12],
                secret_hash=sha256_hex(secret),
                created_by=actor.subject,
                grants=[grant.to_json()],
                created_at=now,
                expires_at=now + WIDGET_LIFETIME,
                # ends with the app's login: a sign-out leaves no usable credential behind,
                # even when the app could not revoke it (its session already ended)
                session_id=actor.session_id,
            )
            await tx.api_tokens.add(token)
            work.touch(api_token(token.id))
            await work.audit(
                ctx,
                "api_token.widget_created",
                account_id=account_id,
                target=("api_token", token.id),
                details={
                    "name": token.name,
                    "permissions": list(grant.permissions),
                    "replaces": str(replaces) if replaces else None,
                },
            )
        return CreatedToken(token, secret_token)

    async def revoke_widget_credential(
        self, account_id: UUID, token_id: UUID, ctx: RequestContext
    ) -> ApiToken:
        """Revoke one of the caller's own widget credentials (404 for any other token, also
        for ordinary API tokens and other users' widget credentials)."""
        actor = ctx.principal
        if actor is None:
            msg = "not authenticated"
            raise ForbiddenError(msg)
        async with self.deps.uow() as work:
            work.account_id = account_id
            token = await work.tx.api_tokens.get(id=token_id, account_id=account_id)
            if token is None or not is_widget_token(token) or token.created_by != actor.subject:
                msg = "widget credential not found"
                raise NotFoundError(msg)
            await self._revoke_in(work, token, account_id, ctx)
        return token

    async def list_personal(self, account_id: UUID, ctx: RequestContext) -> list[ApiToken]:
        """The caller's personal access tokens (active and revoked), newest first."""
        actor = _user_actor(ctx, "only users have personal access tokens")
        found = await self.list_tokens(account_id)
        return [
            t for t in found if t.purpose is TokenPurpose.PERSONAL and t.created_by == actor.subject
        ]

    async def create_personal(
        self,
        account_id: UUID,
        name: str,
        scopes: Sequence[str],
        expires_in_days: int,
        level: Level | None,
        ctx: RequestContext,
    ) -> CreatedToken:
        """Create a personal access token for the caller (ADR 0064).

        ``scopes`` must come from :data:`PERSONAL_SCOPES` (with ``mower.view`` whenever another
        is chosen) and be held by the caller on ``level`` (default: the whole account). The
        token expires after ``expires_in_days`` (1 to :data:`PERSONAL_MAX_LIFETIME_DAYS`). The
        route demands a recent step-up, so an API token can never create one.
        """
        actor = _user_actor(ctx, "only users can create personal access tokens")
        cleaned = " ".join(name.split())
        if not 1 <= len(cleaned) <= 100:
            msg = "token names have 1 to 100 characters"
            raise InvalidInput(msg)
        wanted = sorted(set(scopes))
        unknown = [key for key in wanted if key not in PERSONAL_SCOPES]
        if not wanted or unknown:
            msg = f"choose scopes from {', '.join(PERSONAL_SCOPES)}"
            raise InvalidInput(msg)
        if PERSONAL_VIEW_KEY not in wanted:
            msg = f"{', '.join(wanted)} needs {PERSONAL_VIEW_KEY}"
            raise InvalidInput(msg)
        if not 1 <= expires_in_days <= PERSONAL_MAX_LIFETIME_DAYS:
            msg = f"a personal access token expires after 1 to {PERSONAL_MAX_LIFETIME_DAYS} days"
            raise InvalidInput(msg)
        target = level or Level(LevelType.ACCOUNT, account_id)
        now = self.deps.clock()
        token_id = uuid4()
        secret_token, secret = new_api_token(token_id)
        async with self.deps.uow() as work:
            work.account_id = account_id
            tx = work.tx
            active = [
                t
                for t in await tx.api_tokens.find(account_id=account_id, created_by=actor.subject)
                if t.purpose is TokenPurpose.PERSONAL and t.is_usable(now)
            ]
            if len(active) >= MAX_PERSONAL_TOKENS:
                msg = "too many active personal access tokens, revoke one first"
                raise ConflictError(msg)
            catalogue = await self.deps.catalogue.current(tx)
            keys = validate_token_grant(wanted, target.type, catalogue)
            await self.deps.access.require_level_in_account(target, account_id)
            await self.deps.access.require_holds(tx, actor, account_id, keys, target, catalogue)
            grant = TokenGrant(target, tuple(keys))
            expires_at = now + timedelta(days=expires_in_days)
            token = ApiToken(
                id=token_id,
                account_id=account_id,
                name=cleaned,
                prefix=secret_token[:12],
                secret_hash=sha256_hex(secret),
                created_by=actor.subject,
                grants=[grant.to_json()],
                created_at=now,
                expires_at=expires_at,
                purpose=TokenPurpose.PERSONAL,
            )
            await tx.api_tokens.add(token)
            work.touch(api_token(token.id))
            await work.audit(
                ctx,
                "api_token.personal_created",
                account_id=account_id,
                target=("api_token", token.id),
                details={
                    "name": cleaned,
                    "grants": token.grants,
                    "expires_at": expires_at.isoformat(),
                },
            )
        return CreatedToken(token, secret_token)

    async def revoke_personal(
        self, account_id: UUID, token_id: UUID, ctx: RequestContext
    ) -> ApiToken:
        """Revoke one of the caller's personal access tokens (404 for any other token)."""
        actor = _user_actor(ctx, "only users have personal access tokens")
        async with self.deps.uow() as work:
            work.account_id = account_id
            token = await work.tx.api_tokens.get(id=token_id, account_id=account_id)
            if (
                token is None
                or token.purpose is not TokenPurpose.PERSONAL
                or token.created_by != actor.subject
            ):
                msg = "personal access token not found"
                raise NotFoundError(msg)
            await self._revoke_in(work, token, account_id, ctx)
        return token

    async def _revoke_in(
        self, work: Work, token: ApiToken, account_id: UUID, ctx: RequestContext
    ) -> None:
        """Revoke inside an open unit of work (idempotent)."""
        if token.revoked_at is not None:
            return
        token.revoked_at, token.revoke_reason = self.deps.clock(), "revoked"
        await work.tx.api_tokens.update(token)
        work.touch(api_token(token.id))
        work.emit(
            IDENTITY_API_TOKEN_REVOKED,
            account_id,
            ApiTokenRevoked(token_id=token.id, account_id=account_id, reason="revoked"),
        )
        await work.audit(
            ctx,
            "api_token.revoked",
            account_id=account_id,
            target=("api_token", token.id),
            details={"name": token.name},
        )

    async def purge(self, account_id: UUID, token_id: UUID, ctx: RequestContext) -> ApiToken:
        """Delete a revoked token of the account for good (404 for other accounts' tokens,
        409 for a token that is not revoked yet)."""
        async with self.deps.uow() as work:
            work.account_id = account_id
            token = await work.tx.api_tokens.get(id=token_id, account_id=account_id)
            if token is None:
                msg = "API token not found"
                raise NotFoundError(msg)
            if token.revoked_at is None:
                msg = "revoke the API token before deleting it"
                raise ConflictError(msg)
            await work.tx.api_tokens.delete(id=token.id, account_id=account_id)
            await _purged(work, ctx, account_id, token, bulk=False)
        return token

    async def purge_revoked(self, account_id: UUID, ctx: RequestContext) -> list[ApiToken]:
        """Delete every revoked token of the account for good; active ones stay untouched."""
        async with self.deps.uow() as work:
            work.account_id = account_id
            tokens = await work.tx.api_tokens.find(
                account_id=account_id, order_by="created_at", descending=True
            )
            revoked = [token for token in tokens if token.revoked_at is not None]
            if revoked:
                await work.tx.api_tokens.delete(
                    account_id=account_id, id=In([token.id for token in revoked])
                )
            for token in revoked:
                await _purged(work, ctx, account_id, token, bulk=True)
        return revoked


def _user_actor(ctx: RequestContext, message: str) -> Principal:
    """The calling user or sub-user; API tokens (and anonymous calls) are refused, so a token
    can never see or manage tokens."""
    actor = ctx.principal
    if actor is None or actor.kind not in (PrincipalKind.USER, PrincipalKind.SUBUSER):
        raise ForbiddenError(message)
    return actor


async def _purged(
    work: Work, ctx: RequestContext, account_id: UUID, token: ApiToken, *, bulk: bool
) -> None:
    """Reconcile the deleted token's subject (drops any tuple left over: the account reconcile
    no longer sees it) and audit it by name and prefix, never by secret."""
    work.touch(api_token(token.id))
    await work.audit(
        ctx,
        "api_token.purged",
        account_id=account_id,
        target=("api_token", token.id),
        details={
            "name": token.name,
            "prefix": token.prefix,
            "revoked_at": token.revoked_at.isoformat() if token.revoked_at else None,
            "revoke_reason": token.revoke_reason,
            "bulk": bulk,
        },
    )
