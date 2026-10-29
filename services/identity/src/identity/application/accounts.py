"""Accounts: registration, renaming, deletion with a grace period, reconcile."""

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID, uuid4

from mowdeck_auth.step_up import ensure_recent_auth
from mowdeck_authz import Subject, api_token, user
from mowdeck_contracts.v1 import (
    IDENTITY_ACCOUNT_CREATED,
    IDENTITY_API_TOKEN_REVOKED,
    IDENTITY_USER_CREATED,
    AccountCreated,
    AccountKind,
    ApiTokenRevoked,
    UserChanged,
)

from identity.application.context import SYSTEM, RegistrationMode, RequestContext
from identity.application.deps import Deps
from identity.application.ports import Tx
from identity.application.sync import SyncResult, platform_tuple
from identity.application.work import publish_grants_changed
from identity.domain.credentials import (
    normalize_email,
    slugify,
    validate_password,
    validate_username,
)
from identity.domain.errors import (
    AuthenticationFailed,
    ConflictError,
    ForbiddenError,
    InvalidInput,
    NotFoundError,
)
from identity.domain.models import (
    Account,
    AccountStatus,
    Membership,
    Role,
    Status,
    User,
    UserKind,
)
from identity.domain.permissions import DEFAULT_ROLES


@dataclass(frozen=True, slots=True)
class Registration:
    """Input of a registration."""

    username: str
    password: str
    account_name: str
    display_name: str | None = None
    email: str | None = None
    kind: AccountKind = AccountKind.HOUSEHOLD


@dataclass(frozen=True, slots=True)
class Registered:
    """Output of a registration."""

    account_id: UUID
    user_id: UUID


async def seed_roles(tx: Tx, account_id: UUID, now: datetime) -> list[Role]:
    """Create Owner, Admin, Manager, Operator and Viewer in a new account."""
    roles = []
    for template in DEFAULT_ROLES:
        role = Role(
            id=uuid4(),
            account_id=account_id,
            key=template.key,
            name=template.name,
            kind=template.kind,
            permissions=list(template.permissions),
            description=template.description,
            created_at=now,
            updated_at=now,
        )
        roles.append(await tx.roles.add(role))
    return roles


async def ensure_unique_user(tx: Tx, username_norm: str, email_norm: str | None) -> None:
    """409 if the username or e-mail is taken (installation wide)."""
    if await tx.users.get(username_norm=username_norm) is not None:
        msg = "this username is taken"
        raise ConflictError(msg)
    if email_norm is not None and await tx.users.get(email_norm=email_norm) is not None:
        msg = "this e-mail address is taken"
        raise ConflictError(msg)


async def account_subjects(tx: Tx, account_id: UUID) -> set[Subject]:
    """Every subject that may hold tuples in the account (members and API tokens)."""
    subjects: set[Subject] = {
        user(m.user_id) for m in await tx.memberships.find(account_id=account_id)
    }
    subjects |= {api_token(t.id) for t in await tx.api_tokens.find(account_id=account_id)}
    return subjects


def validate_name(name: str) -> str:
    """Account names: 1 to 200 characters after trimming."""
    cleaned = name.strip()
    if not 1 <= len(cleaned) <= 200:
        msg = "account names have 1 to 200 characters"
        raise InvalidInput(msg)
    return cleaned


class AccountService:
    """Use cases around accounts."""

    def __init__(self, deps: Deps) -> None:
        self.deps = deps

    async def register(
        self, data: Registration, ctx: RequestContext, *, ignore_mode: bool = False
    ) -> Registered:
        """Create an account with its owner, the default roles and the owner's tuples.

        ``ignore_mode`` (operator CLI only) registers even when registration is closed.
        """
        username_norm = validate_username(data.username)
        validate_password(data.password, username=data.username)
        email_norm = normalize_email(data.email)
        name = validate_name(data.account_name)
        password_hash = await self.deps.hasher.hash(data.password)
        now = self.deps.clock()
        async with self.deps.uow() as work:
            tx = work.tx
            if (
                not ignore_mode
                and self.deps.config.registration is not RegistrationMode.OPEN
                and await tx.users.count() > 0
            ):
                msg = "registration is closed on this installation"
                raise ForbiddenError(msg)
            await ensure_unique_user(tx, username_norm, email_norm)
            account_id, user_id = uuid4(), uuid4()
            work.account_id = account_id
            await tx.accounts.add(
                Account(
                    id=account_id,
                    name=name,
                    slug=slugify(name, account_id.hex[:8]),
                    kind=data.kind.value,
                    status=AccountStatus.ACTIVE,
                    plan=self.deps.config.default_plan,
                    created_at=now,
                )
            )
            await tx.users.add(
                User(
                    id=user_id,
                    username=data.username.strip(),
                    username_norm=username_norm,
                    display_name=(data.display_name or data.username).strip()[:200],
                    kind=UserKind.PERSON,
                    status=Status.ACTIVE,
                    created_at=now,
                    password_hash=password_hash,
                    password_changed_at=now,
                    email=data.email.strip() if data.email and email_norm else None,
                    email_norm=email_norm,
                )
            )
            await tx.memberships.add(
                Membership(
                    id=uuid4(),
                    account_id=account_id,
                    user_id=user_id,
                    status=Status.ACTIVE,
                    created_at=now,
                    is_owner=True,
                )
            )
            await seed_roles(tx, account_id, now)
            work.touch(user(user_id))
            work.ensure(platform_tuple(account_id))
            actor = (
                ctx
                if ctx.principal
                else RequestContext(
                    ip=ctx.ip, correlation_id=ctx.correlation_id, user_agent=ctx.user_agent
                )
            )
            entry = actor.audit_entry(
                at=now,
                action="account.registered",
                account_id=account_id,
                target_type="account",
                target_id=str(account_id),
                details={"owner": str(user_id), "username": username_norm, "kind": data.kind.value},
            )
            entry.actor_subject, entry.actor_kind = f"user:{user_id}", "user"
            await tx.audit.add(entry)
            work.emit(
                IDENTITY_ACCOUNT_CREATED,
                account_id,
                AccountCreated(
                    account_id=account_id, name=name, kind=data.kind, owner_user_id=user_id
                ),
            )
            work.emit(
                IDENTITY_USER_CREATED,
                account_id,
                UserChanged(user_id=user_id, account_id=account_id, username=username_norm),
            )
        return Registered(account_id, user_id)

    async def get(self, account_id: UUID) -> Account:
        """The account (404 if missing)."""
        async with self.deps.store.transaction() as tx:
            acct = await tx.accounts.get(id=account_id)
        if acct is None:
            msg = "account not found"
            raise NotFoundError(msg)
        return acct

    async def rename(self, account_id: UUID, name: str, ctx: RequestContext) -> Account:
        """Change the display name of the account."""
        cleaned = validate_name(name)
        async with self.deps.uow() as work:
            acct = await self._load(work.tx, account_id)
            old, acct.name = acct.name, cleaned
            await work.tx.accounts.update(acct)
            await work.audit(
                ctx,
                "account.renamed",
                account_id=account_id,
                target=("account", account_id),
                details={"from": old, "to": cleaned},
            )
        return acct

    async def _load(self, tx: Tx, account_id: UUID) -> Account:
        acct = await tx.accounts.get(id=account_id)
        if acct is None:
            msg = "account not found"
            raise NotFoundError(msg)
        return acct

    async def request_deletion(
        self, account_id: UUID, password: str | None, ctx: RequestContext
    ) -> Account:
        """Lock the account for the grace period (owner only, password re-entry or a recent
        step-up confirmation, ADR 0057).

        API tokens are revoked and the sessions of other members end; the owner keeps access
        to restore the account. The purge after the grace period is a later job.
        """
        if ctx.principal is None:
            msg = "login required"
            raise ForbiddenError(msg)
        if password is None:
            ensure_recent_auth(ctx.principal, now=self.deps.clock())
        async with self.deps.store.transaction() as tx:
            actor = await tx.users.get(id=ctx.principal.subject)
        if actor is None or (
            password is not None
            and (
                actor.password_hash is None
                or not await self.deps.hasher.verify(actor.password_hash, password)
            )
        ):
            raise AuthenticationFailed("password re-entry failed")
        now = self.deps.clock()
        async with self.deps.uow() as work:
            tx = work.tx
            work.account_id = account_id
            acct = await self._load(tx, account_id)
            if acct.status is AccountStatus.DELETING:
                msg = "the account is already scheduled for deletion"
                raise ConflictError(msg)
            acct.status = AccountStatus.DELETING
            acct.delete_after = now + self.deps.config.deletion_grace
            await tx.accounts.update(acct)
            for token in await tx.api_tokens.find(account_id=account_id, revoked_at=None):
                token.revoked_at, token.revoke_reason = now, "account deletion"
                await tx.api_tokens.update(token)
                work.emit(
                    IDENTITY_API_TOKEN_REVOKED,
                    account_id,
                    ApiTokenRevoked(
                        token_id=token.id, account_id=account_id, reason="account deletion"
                    ),
                )
            owners = {
                m.user_id for m in await tx.memberships.find(account_id=account_id, is_owner=True)
            }
            for session in await tx.sessions.find(account_id=account_id, revoked_at=None):
                if session.user_id not in owners:
                    session.revoked_at, session.revoke_reason = now, "account deletion"
                    await tx.sessions.update(session)
            work.touch(*await account_subjects(tx, account_id))
            await work.audit(
                ctx,
                "account.deletion_requested",
                account_id=account_id,
                target=("account", account_id),
                details={"delete_after": acct.delete_after.isoformat()},
            )
        return acct

    async def restore(self, account_id: UUID, ctx: RequestContext) -> Account:
        """Undo a deletion request during the grace period (owner only)."""
        async with self.deps.uow() as work:
            work.account_id = account_id
            acct = await self._load(work.tx, account_id)
            if acct.status is not AccountStatus.DELETING:
                msg = "the account is not scheduled for deletion"
                raise ConflictError(msg)
            acct.status, acct.delete_after = AccountStatus.ACTIVE, None
            await work.tx.accounts.update(acct)
            work.touch(*await account_subjects(work.tx, account_id))
            await work.audit(
                ctx, "account.restored", account_id=account_id, target=("account", account_id)
            )
        return acct

    async def promote_owner(
        self, account_id: UUID, username: str, ctx: RequestContext, *, keep_current: bool = False
    ) -> Membership:
        """Make a member the owner (operator CLI). Former owners become admins unless kept."""
        async with self.deps.uow() as work:
            work.account_id = account_id
            tx = work.tx
            await self._load(tx, account_id)
            target = await tx.users.get(username_norm=validate_username(username))
            membership = (
                await tx.memberships.get(account_id=account_id, user_id=target.id)
                if target
                else None
            )
            if target is None or membership is None:
                msg = "no such member in this account"
                raise NotFoundError(msg)
            former = []
            if not keep_current:
                for other in await tx.memberships.find(account_id=account_id, is_owner=True):
                    if other.user_id != target.id:
                        other.is_owner, other.is_admin = False, True
                        await tx.memberships.update(other)
                        work.touch(user(other.user_id))
                        former.append(str(other.user_id))
            membership.is_owner, membership.status = True, Status.ACTIVE
            await tx.memberships.update(membership)
            work.touch(user(target.id))
            await work.audit(
                ctx,
                "account.owner_promoted",
                account_id=account_id,
                target=("user", target.id),
                details={"former_owners": former},
            )
        return membership

    async def reconcile_account(self, account_id: UUID, ctx: RequestContext) -> SyncResult:
        """Make OpenFGA match the database for every subject of the account."""
        async with self.deps.uow() as work:
            work.account_id = account_id
            await self._load(work.tx, account_id)
            work.touch(*await account_subjects(work.tx, account_id))
            work.ensure(platform_tuple(account_id))
        async with self.deps.uow() as audit_work:
            await audit_work.audit(
                ctx,
                "authz.reconciled",
                account_id=account_id,
                target=("account", account_id),
                details={"writes": work.result.writes, "deletes": work.result.deletes},
            )
        return work.result

    async def reconcile_all(self) -> SyncResult:
        """Reconcile every account (periodic job and CLI); one transaction per account."""
        total = SyncResult()
        async with self.deps.store.transaction() as tx:
            account_ids = [a.id for a in await tx.accounts.find()]
        for account_id in account_ids:
            async with self.deps.store.transaction() as tx:
                catalogue = await self.deps.catalogue.current(tx)
                subjects = await account_subjects(tx, account_id)
                result = await self.deps.sync.reconcile(
                    tx, subjects, catalogue, extra_writes=[platform_tuple(account_id)]
                )
            total.merge(result)
            await publish_grants_changed(self.deps.events, account_id, result.changed_subjects)
        if total.writes or total.deletes:
            async with self.deps.uow() as work:
                await work.audit(
                    SYSTEM,
                    "authz.reconciled",
                    details={"writes": total.writes, "deletes": total.deletes, "scope": "all"},
                )
        return total
