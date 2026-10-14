"""Members and sub-users of an account.

* Sub-users are created by the account with a username and a password (no e-mail needed) and
  belong to it. Persons join through registration (and invitations later).
* Disabling a sub-user disables the user; disabling a person disables the membership in this
  account only. Either way the sessions in the account end, the API tokens the user created in
  the account are revoked and the tuples are removed.
* The owner cannot be disabled or removed; only the owner can disable or remove an admin;
  nobody disables or removes themselves.
"""

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID, uuid4

from mowdeck_authz import api_token, user
from mowdeck_contracts.v1 import (
    IDENTITY_API_TOKEN_REVOKED,
    IDENTITY_USER_CREATED,
    IDENTITY_USER_DELETED,
    IDENTITY_USER_DISABLED,
    ApiTokenRevoked,
    UserChanged,
)
from mowdeck_service_kit.entitlements import require_limit

from identity.application.accounts import ensure_unique_user
from identity.application.context import RequestContext
from identity.application.deps import Deps
from identity.application.ports import Tx
from identity.application.roles import AssignmentSpec, assign_role
from identity.application.work import Work
from identity.domain.credentials import normalize_email, validate_password, validate_username
from identity.domain.errors import ForbiddenError, InvalidInput, NotFoundError
from identity.domain.models import (
    Membership,
    RoleAssignment,
    Status,
    SubjectType,
    User,
    UserKind,
)


@dataclass(frozen=True, slots=True)
class NewSubUser:
    """Input for a sub-user."""

    username: str
    password: str
    display_name: str | None = None
    email: str | None = None
    must_change_password: bool = False
    assignments: Sequence[AssignmentSpec] = ()


@dataclass(frozen=True, slots=True)
class MemberView:
    """A user as a member of one account."""

    user: User
    membership: Membership
    assignments: list[RoleAssignment] = field(default_factory=list)

    @property
    def subuser(self) -> bool:
        """True if the account owns the user."""
        return (
            self.user.kind is UserKind.SUBUSER
            and self.user.owner_account_id == self.membership.account_id
        )


class UserService:
    """Use cases around members and sub-users."""

    def __init__(self, deps: Deps) -> None:
        self.deps = deps

    async def _view(self, tx: Tx, membership: Membership) -> MemberView | None:
        found = await tx.users.get(id=membership.user_id)
        if found is None:
            return None
        assignments = await tx.assignments.find(
            account_id=membership.account_id,
            subject_type=SubjectType.USER.value,
            subject_id=found.id,
            order_by="created_at",
        )
        return MemberView(found, membership, assignments)

    async def list_members(self, account_id: UUID) -> list[MemberView]:
        """Every member of the account (persons and sub-users)."""
        async with self.deps.store.transaction() as tx:
            views = [
                await self._view(tx, m)
                for m in await tx.memberships.find(account_id=account_id, order_by="created_at")
            ]
        return [v for v in views if v is not None]

    async def get_member(self, account_id: UUID, user_id: UUID) -> MemberView:
        """One member (404 if the user is not a member of this account)."""
        async with self.deps.store.transaction() as tx:
            membership = await tx.memberships.get(account_id=account_id, user_id=user_id)
            view = await self._view(tx, membership) if membership else None
        if view is None:
            msg = "user not found"
            raise NotFoundError(msg)
        return view

    async def create_subuser(
        self, account_id: UUID, data: NewSubUser, ctx: RequestContext
    ) -> MemberView:
        """Create a sub-user with optional role assignments (anti-escalation applies)."""
        username_norm = validate_username(data.username)
        validate_password(data.password, username=data.username)
        email_norm = normalize_email(data.email)
        password_hash = await self.deps.hasher.hash(data.password)
        now = self.deps.clock()
        async with self.deps.uow() as work:
            work.account_id = account_id
            tx = work.tx
            if self.deps.entitlements is not None:
                members = await tx.memberships.count(account_id=account_id)
                await require_limit(self.deps.entitlements, account_id, "max_users", members)
            await ensure_unique_user(tx, username_norm, email_norm)
            created = User(
                id=uuid4(),
                username=data.username.strip(),
                username_norm=username_norm,
                display_name=(data.display_name or data.username).strip()[:200],
                kind=UserKind.SUBUSER,
                status=Status.ACTIVE,
                created_at=now,
                password_hash=password_hash,
                email=data.email.strip() if data.email and email_norm else None,
                email_norm=email_norm,
                owner_account_id=account_id,
                created_by=ctx.principal.subject if ctx.principal else None,
                must_change_password=data.must_change_password,
            )
            await tx.users.add(created)
            membership = Membership(uuid4(), account_id, created.id, Status.ACTIVE, now)
            await tx.memberships.add(membership)
            work.touch(user(created.id))
            await work.audit(
                ctx,
                "user.created",
                account_id=account_id,
                target=("user", created.id),
                details={"username": username_norm, "subuser": True},
            )
            catalogue = await self.deps.catalogue.current(tx)
            assignments = [
                await assign_role(
                    self.deps, work, ctx, account_id, SubjectType.USER, created.id, spec, catalogue
                )
                for spec in data.assignments
            ]
            work.emit(
                IDENTITY_USER_CREATED,
                account_id,
                UserChanged(
                    user_id=created.id, account_id=account_id, username=username_norm, subuser=True
                ),
            )
        return MemberView(created, membership, assignments)

    async def _target(
        self, work: Work, account_id: UUID, user_id: UUID, ctx: RequestContext
    ) -> tuple[User, Membership]:
        """Load a member and apply the guards for disabling or removing it."""
        tx = work.tx
        membership = await tx.memberships.get(account_id=account_id, user_id=user_id)
        found = await tx.users.get(id=user_id) if membership else None
        if membership is None or found is None:
            msg = "user not found"
            raise NotFoundError(msg)
        actor = ctx.principal
        if actor is None:
            msg = "login required"
            raise ForbiddenError(msg)
        if actor.subject == user_id:
            msg = "you cannot do this to yourself"
            raise InvalidInput(msg)
        if membership.is_owner:
            msg = "the owner cannot be disabled or removed"
            raise ForbiddenError(msg)
        if membership.is_admin:
            actor_membership = await tx.memberships.get(
                account_id=account_id, user_id=actor.subject
            )
            if actor_membership is None or not actor_membership.is_owner:
                msg = "only the owner can disable or remove an admin"
                raise ForbiddenError(msg)
        return found, membership

    async def _cascade(
        self, work: Work, account_id: UUID, user_id: UUID, reason: str, *, all_sessions: bool
    ) -> None:
        """End sessions and revoke the API tokens the user created in the account."""
        tx = work.tx
        now = self.deps.clock()
        filters: dict[str, Any] = {"user_id": user_id, "revoked_at": None}
        if not all_sessions:
            filters["account_id"] = account_id
        for session in await tx.sessions.find(**filters):
            session.revoked_at, session.revoke_reason = now, reason
            await tx.sessions.update(session)
        for token in await tx.api_tokens.find(
            account_id=account_id, created_by=user_id, revoked_at=None
        ):
            token.revoked_at, token.revoke_reason = now, reason
            await tx.api_tokens.update(token)
            work.touch(api_token(token.id))
            work.emit(
                IDENTITY_API_TOKEN_REVOKED,
                account_id,
                ApiTokenRevoked(token_id=token.id, account_id=account_id, reason=reason),
            )
        work.touch(user(user_id))

    async def set_disabled(
        self, account_id: UUID, user_id: UUID, disabled: bool, ctx: RequestContext
    ) -> MemberView:
        """Disable (with cascade) or enable a member."""
        async with self.deps.uow() as work:
            work.account_id = account_id
            found, membership = await self._target(work, account_id, user_id, ctx)
            status = Status.DISABLED if disabled else Status.ACTIVE
            owned = found.kind is UserKind.SUBUSER and found.owner_account_id == account_id
            if owned:
                found.status = status
                await work.tx.users.update(found)
            membership.status = status
            await work.tx.memberships.update(membership)
            if disabled:
                await self._cascade(work, account_id, user_id, "user disabled", all_sessions=owned)
                work.emit(
                    IDENTITY_USER_DISABLED,
                    account_id,
                    UserChanged(
                        user_id=user_id,
                        account_id=account_id,
                        username=found.username_norm,
                        subuser=owned,
                    ),
                )
            else:
                work.touch(user(user_id))
            await work.audit(
                ctx,
                "user.disabled" if disabled else "user.enabled",
                account_id=account_id,
                target=("user", user_id),
                details={"subuser": owned},
            )
            assignments = await work.tx.assignments.find(
                account_id=account_id, subject_type=SubjectType.USER.value, subject_id=user_id
            )
        return MemberView(found, membership, assignments)

    async def rename(
        self, account_id: UUID, user_id: UUID, display_name: str, ctx: RequestContext
    ) -> MemberView:
        """Change the display name of a member (sub-users of the account, or yourself)."""
        cleaned = display_name.strip()
        if not 1 <= len(cleaned) <= 200:
            msg = "display names have 1 to 200 characters"
            raise InvalidInput(msg)
        async with self.deps.uow() as work:
            membership = await work.tx.memberships.get(account_id=account_id, user_id=user_id)
            found = await work.tx.users.get(id=user_id) if membership else None
            if membership is None or found is None:
                msg = "user not found"
                raise NotFoundError(msg)
            is_self = ctx.principal is not None and ctx.principal.subject == user_id
            if not is_self and found.owner_account_id != account_id:
                msg = "only sub-users of this account can be renamed by others"
                raise ForbiddenError(msg)
            found.display_name = cleaned
            await work.tx.users.update(found)
            await work.audit(
                ctx,
                "user.renamed",
                account_id=account_id,
                target=("user", user_id),
                details={"display_name": cleaned},
            )
        return MemberView(found, membership)

    async def set_password(
        self, account_id: UUID, user_id: UUID, password: str, ctx: RequestContext
    ) -> None:
        """Set a temporary password of a sub-user of this account; its sessions end."""
        async with self.deps.store.transaction() as tx:
            found = await tx.users.get(id=user_id)
            membership = await tx.memberships.get(account_id=account_id, user_id=user_id)
        if found is None or membership is None:
            msg = "user not found"
            raise NotFoundError(msg)
        if found.owner_account_id != account_id:
            msg = "only passwords of sub-users of this account can be set"
            raise ForbiddenError(msg)
        validate_password(password, username=found.username)
        new_hash = await self.deps.hasher.hash(password)
        now = self.deps.clock()
        async with self.deps.uow() as work:
            fresh = await work.tx.users.get(id=user_id)
            if fresh is None:
                msg = "user not found"
                raise NotFoundError(msg)
            fresh.password_hash, fresh.must_change_password = new_hash, True
            await work.tx.users.update(fresh)
            for session in await work.tx.sessions.find(user_id=user_id, revoked_at=None):
                session.revoked_at, session.revoke_reason = now, "password reset"
                await work.tx.sessions.update(session)
            await work.audit(
                ctx, "user.password_reset", account_id=account_id, target=("user", user_id)
            )

    async def remove(self, account_id: UUID, user_id: UUID, ctx: RequestContext) -> None:
        """Delete a sub-user of the account, or remove a person from the account."""
        async with self.deps.uow() as work:
            work.account_id = account_id
            tx = work.tx
            found, membership = await self._target(work, account_id, user_id, ctx)
            owned = found.kind is UserKind.SUBUSER and found.owner_account_id == account_id
            await self._cascade(work, account_id, user_id, "user removed", all_sessions=owned)
            await tx.assignments.delete(
                account_id=account_id, subject_type=SubjectType.USER.value, subject_id=user_id
            )
            await tx.memberships.delete(id=membership.id)
            if owned:
                await tx.assignments.delete(subject_type=SubjectType.USER.value, subject_id=user_id)
                await tx.memberships.delete(user_id=user_id)
                await tx.sessions.delete(user_id=user_id)
                await tx.login_attempts.delete(username_norm=found.username_norm)
                await tx.users.delete(id=user_id)
            await work.audit(
                ctx,
                "user.deleted" if owned else "user.removed",
                account_id=account_id,
                target=("user", user_id),
                details={"username": found.username_norm, "subuser": owned},
            )
            work.emit(
                IDENTITY_USER_DELETED,
                account_id,
                UserChanged(
                    user_id=user_id,
                    account_id=account_id,
                    username=found.username_norm,
                    subuser=owned,
                ),
            )
