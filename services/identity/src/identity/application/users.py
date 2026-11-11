# SPDX-FileCopyrightText: 2026 Lukas Körber
# SPDX-License-Identifier: AGPL-3.0-only

"""Members and sub-users of an account.

* Sub-users are created by the account with a username and a password (no e-mail needed) and
  belong to it. Persons join through registration (and invitations later).
* Disabling a sub-user disables the user; disabling a person disables the membership in this
  account only. Either way the sessions in the account end, the API tokens the user created in
  the account are revoked and the tuples are removed.
* The owner cannot be disabled or removed; only the owner can disable or remove an admin;
  nobody disables or removes themselves through the administration.
* A member who is not the owner can delete themselves (``delete_self``, ADR 0082): the user row,
  every credential and session, the membership, the role assignments and tuples go at once, the
  audit rows keep no personal field, and ``identity.user.deleted`` tells the other services.
  What a deleted user leaves is in one place, :func:`delete_user_rows`, shared with the removal
  of a sub-user and the erasure of an account.
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

from identity.application.accounts import ensure_not_scheduled_for_deletion, ensure_unique_user
from identity.application.api_tokens import is_widget_token
from identity.application.context import RequestContext
from identity.application.deps import Deps
from identity.application.ports import Tx
from identity.application.roles import AssignmentSpec, assign_role
from identity.application.work import Work
from identity.domain.credentials import normalize_email, validate_password, validate_username
from identity.domain.erasure import SelfDeletion, self_deletion, self_deletion_blocker
from identity.domain.errors import ConflictError, ForbiddenError, InvalidInput, NotFoundError
from identity.domain.models import (
    ApiToken,
    Membership,
    RoleAssignment,
    Status,
    SubjectType,
    TokenPurpose,
    User,
    UserKind,
)

OWNER_DELETES_THE_ACCOUNT = (
    "the owner cannot leave the account: delete the account instead "
    "(DELETE /api/v1/identity/accounts/{account_id})"
)
"""Why an owner is not deleted as a user; names the operation that applies."""


async def delete_user_rows(tx: Tx, found: User) -> None:
    """Everything Identity holds of one user, then the user: sessions, the tour progress, known
    devices, passkeys and open ceremonies, app login codes, linked providers and their open
    redirects, role assignments, memberships and the login attempts under the username.

    The one list of a user's rows (PostgreSQL cascades most of them from ``users``; the memory
    store does not, and a list that is spelled out does not depend on a foreign key somebody
    forgets). Used by the removal of a sub-user, by "delete me" and by the account erasure.
    """
    user_id = found.id
    await tx.sessions.delete(user_id=user_id)
    await tx.onboarding.delete(user_id=user_id)
    await tx.known_devices.delete(user_id=user_id)
    await tx.passkeys.delete(user_id=user_id)
    await tx.webauthn_challenges.delete(user_id=user_id)
    await tx.app_login_codes.delete(user_id=user_id)
    await tx.external_identities.delete(user_id=user_id)
    await tx.oauth_states.delete(user_id=user_id)
    await tx.assignments.delete(subject_type=SubjectType.USER.value, subject_id=user_id)
    await tx.memberships.delete(user_id=user_id)
    await tx.login_attempts.delete(username_norm=found.username_norm)
    await tx.users.delete(id=user_id)


def is_personal_token(token: ApiToken) -> bool:
    """A token that is its creator's own and not the account's: a personal access token
    (ADR 0064) or an iOS widget credential (ADR 0059, named after the creator's device)."""
    return token.purpose is TokenPurpose.PERSONAL or is_widget_token(token)


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
            await ensure_not_scheduled_for_deletion(tx, account_id)
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
                password_changed_at=now,
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
            fresh.password_changed_at = self.deps.clock()
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
                await delete_user_rows(tx, found)
            await work.audit(
                ctx,
                "user.deleted" if owned else "user.removed",
                account_id=account_id,
                target=("user", user_id),
                details={"username": found.username_norm, "subuser": owned},
            )
            _emit_deleted(work, account_id, found, owned=owned)

    async def delete_self(self, account_id: UUID, ctx: RequestContext) -> None:
        """The caller deletes themselves (ADR 0082, "delete me"): a member who is not the owner.

        At once and for good, without a grace period: nothing of the account is lost (what the
        user created belongs to the account and stays, without their name), so there is nothing
        to restore. In one unit of work: every session ends, the API tokens the user created are
        revoked and their personal ones (personal access tokens, widget credentials) deleted,
        the user's rows go (:func:`delete_user_rows`), the tuples are reconciled away, and the
        audit rows about the user lose their personal fields (the rows stay, the last one is
        ``user.self_deleted``). ``identity.user.deleted`` goes out after the commit: the other
        services drop the user's rows and their name.

        The caller's re-authentication is the use case above this one
        (:class:`~identity.application.self_deletion.SelfDeletionService`). The owner is refused
        here too (409): the rule is checked in the transaction that deletes.
        """
        actor = ctx.principal
        if actor is None:
            msg = "login required"
            raise ForbiddenError(msg)
        async with self.deps.uow() as work:
            work.account_id = account_id
            tx = work.tx
            memberships = await tx.memberships.find(user_id=actor.subject)
            member = any(m.account_id == account_id for m in memberships)
            found = await tx.users.get(id=actor.subject) if member else None
            if found is None:
                msg = "user not found"
                raise NotFoundError(msg)
            # The whole rule again, in the transaction that deletes: the person is removed
            # everywhere, so every membership counts, not only the one of this account.
            blocker = self_deletion_blocker(memberships, account_id)
            if blocker is not None:
                raise ConflictError(blocker)
            if self_deletion(memberships, account_id) is not SelfDeletion.USER:
                raise ConflictError(OWNER_DELETES_THE_ACCOUNT)
            owned = found.kind is UserKind.SUBUSER and found.owner_account_id == account_id
            await self._cascade(work, account_id, found.id, "user deleted", all_sessions=True)
            for token in await tx.api_tokens.find(account_id=account_id, created_by=found.id):
                if is_personal_token(token):
                    await tx.api_tokens.delete(id=token.id)
            await work.audit(
                ctx,
                "user.self_deleted",
                account_id=account_id,
                target=("user", found.id),
                details={"subuser": owned},
            )
            await delete_user_rows(tx, found)
            await tx.scrub_user_audit(found.id)
            _emit_deleted(work, account_id, found, owned=owned, reason="self")


def _emit_deleted(
    work: Work, account_id: UUID, found: User, *, owned: bool, reason: str | None = None
) -> None:
    """``identity.user.deleted`` after the commit: the other services drop what is the user's."""
    work.emit(
        IDENTITY_USER_DELETED,
        account_id,
        UserChanged(
            user_id=found.id,
            account_id=account_id,
            username=found.username_norm,
            subuser=owned,
            reason=reason,
        ),
    )
