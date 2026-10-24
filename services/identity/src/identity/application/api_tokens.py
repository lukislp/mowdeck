"""API tokens: long-lived credentials with explicit grants, capped at the creator's rights.

A token is its own subject (``api_token:<id>``) with ``member`` on its account and ``grant_*``
tuples for its grants. It never inherits the creator's rights, can never hold administrative
keys (``users.manage``, ``billing.manage``, ``audit.view``), and is revoked when its creator is
disabled or removed. The secret is shown once; only its SHA-256 is stored.

Revoked tokens stay listed until the owner deletes them for good (``purge`` for one,
``purge_revoked`` for all of the account). Only revoked tokens can be deleted: an active token
is revoked first, so its ``api_token.revoked`` event always goes out. The audit rows of a deleted
token stay (``audit_log`` is append-only and refers to the token by id only, no foreign key).
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID, uuid4

from mowdeck_authz import api_token
from mowdeck_contracts.v1 import IDENTITY_API_TOKEN_REVOKED, ApiTokenRevoked, PrincipalKind

from identity.application.context import RequestContext
from identity.application.deps import Deps
from identity.application.ports import In
from identity.application.work import Work
from identity.domain.credentials import new_api_token, sha256_hex
from identity.domain.errors import ConflictError, ForbiddenError, InvalidInput, NotFoundError
from identity.domain.models import ApiToken, TokenGrant
from identity.domain.permissions import validate_token_grant


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
