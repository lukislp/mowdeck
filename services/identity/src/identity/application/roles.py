"""Roles (permission bundles of an account) and role assignments at a level.

* Owner and Admin are fixed relations. Admin is assigned through its system role at account
  level by the owner only; Owner is never assigned (ownership transfer is a later flow).
* Creating or editing a role requires holding every key of it on the whole account.
* Assigning a role at a level requires holding every effective key of it on that level, and the
  level must belong to the account. Keys that do not exist on a level are not granted there
  (``fleet.bulk_command`` on a single mower, account keys below the account).
* Editing a role reconciles every subject that holds it; deleting is refused while assigned.
"""

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any
from uuid import UUID, uuid4

from mowdeck_contracts.v1 import Principal

from identity.application.context import RequestContext
from identity.application.deps import Deps
from identity.application.sync import subject_of
from identity.application.work import Work
from identity.domain.errors import ConflictError, ForbiddenError, InvalidInput, NotFoundError
from identity.domain.models import (
    Level,
    LevelType,
    Role,
    RoleAssignment,
    RoleKind,
    SubjectType,
)
from identity.domain.permissions import (
    Catalogue,
    validate_role_permissions,
)

_KEY_CHARS = frozenset("abcdefghijklmnopqrstuvwxyz0123456789_-")


@dataclass(frozen=True, slots=True)
class AssignmentSpec:
    """A role at a level, as requested."""

    role_id: UUID
    level: Level


def _role_key(name: str) -> str:
    key = "".join(c if c in _KEY_CHARS else "-" for c in name.strip().casefold()).strip("-")
    return key[:60] or "role"


def _actor(ctx: RequestContext) -> Principal:
    if ctx.principal is None:
        msg = "login required"
        raise ForbiddenError(msg)
    return ctx.principal


async def assign_role(
    deps: Deps,
    work: Work,
    ctx: RequestContext,
    account_id: UUID,
    subject_type: SubjectType,
    subject_id: UUID,
    spec: AssignmentSpec,
    catalogue: Catalogue,
) -> RoleAssignment:
    """Validate (anti-escalation, ownership of the level) and add one assignment."""
    actor = _actor(ctx)
    tx = work.tx
    role = await tx.roles.get(id=spec.role_id, account_id=account_id)
    if role is None:
        msg = "role not found"
        raise NotFoundError(msg)
    if subject_type is not SubjectType.USER:
        msg = "roles are assigned to users; API tokens carry their own grants"
        raise InvalidInput(msg)
    membership = await tx.memberships.get(account_id=account_id, user_id=subject_id)
    if membership is None:
        msg = "user not found"
        raise NotFoundError(msg)
    await deps.access.require_level_in_account(spec.level, account_id)
    if role.kind is RoleKind.SYSTEM_OWNER:
        msg = "the owner role cannot be assigned"
        raise ForbiddenError(msg)
    if role.kind is RoleKind.SYSTEM_ADMIN:
        actor_membership = await tx.memberships.get(account_id=account_id, user_id=actor.subject)
        if actor_membership is None or not actor_membership.is_owner:
            msg = "only the owner can assign the admin role"
            raise ForbiddenError(msg)
        if spec.level.type is not LevelType.ACCOUNT:
            msg = "admin is an account level role"
            raise InvalidInput(msg)
        membership.is_admin = True
        await tx.memberships.update(membership)
    else:
        effective = catalogue.effective(role.permissions, spec.level.type, SubjectType.USER)
        await deps.access.require_holds(tx, actor, account_id, effective, spec.level, catalogue)
    existing = await tx.assignments.get(
        account_id=account_id,
        subject_type=subject_type.value,
        subject_id=subject_id,
        role_id=role.id,
        level_type=spec.level.type.value,
        level_id=spec.level.id,
    )
    if existing is not None:
        return existing
    assignment = RoleAssignment(
        id=uuid4(),
        account_id=account_id,
        subject_type=subject_type,
        subject_id=subject_id,
        role_id=role.id,
        level_type=spec.level.type,
        level_id=spec.level.id,
        created_at=deps.clock(),
        created_by=actor.subject,
    )
    await tx.assignments.add(assignment)
    work.touch(subject_of(subject_type, subject_id))
    await work.audit(
        ctx,
        "role.assigned",
        account_id=account_id,
        target=(subject_type.value, subject_id),
        details={
            "role": role.key,
            "level": str(spec.level),
            "permissions": catalogue.effective(role.permissions, spec.level.type, SubjectType.USER),
        },
    )
    return assignment


class RoleService:
    """Use cases around roles and assignments."""

    def __init__(self, deps: Deps) -> None:
        self.deps = deps

    async def list_roles(self, account_id: UUID) -> list[Role]:
        """Roles of the account, system roles first."""
        async with self.deps.store.transaction() as tx:
            roles = await tx.roles.find(account_id=account_id)
        order = {
            RoleKind.SYSTEM_OWNER: 0,
            RoleKind.SYSTEM_ADMIN: 1,
            RoleKind.DEFAULT: 2,
            RoleKind.CUSTOM: 3,
        }
        return sorted(roles, key=lambda r: (order[r.kind], r.name.casefold()))

    async def create_role(
        self,
        account_id: UUID,
        name: str,
        permissions: Iterable[str],
        description: str,
        ctx: RequestContext,
    ) -> Role:
        """A custom role; the actor must hold every key on the whole account."""
        actor = _actor(ctx)
        cleaned = name.strip()
        if not 1 <= len(cleaned) <= 100:
            msg = "role names have 1 to 100 characters"
            raise InvalidInput(msg)
        now = self.deps.clock()
        async with self.deps.uow() as work:
            catalogue = await self.deps.catalogue.current(work.tx)
            keys = validate_role_permissions(permissions, catalogue)
            level = Level(LevelType.ACCOUNT, account_id)
            await self.deps.access.require_holds(work.tx, actor, account_id, keys, level, catalogue)
            key = _role_key(cleaned)
            if await work.tx.roles.get(account_id=account_id, key=key) is not None:
                msg = "a role with this name exists"
                raise ConflictError(msg)
            role = Role(
                id=uuid4(),
                account_id=account_id,
                key=key,
                name=cleaned,
                kind=RoleKind.CUSTOM,
                permissions=keys,
                description=description.strip()[:500],
                created_at=now,
                updated_at=now,
            )
            await work.tx.roles.add(role)
            await work.audit(
                ctx,
                "role.created",
                account_id=account_id,
                target=("role", role.id),
                details={"key": key, "permissions": keys},
            )
        return role

    async def update_role(
        self,
        account_id: UUID,
        role_id: UUID,
        ctx: RequestContext,
        *,
        name: str | None = None,
        permissions: Iterable[str] | None = None,
        description: str | None = None,
    ) -> Role:
        """Rename or change the keys of a role; every holder is reconciled."""
        actor = _actor(ctx)
        async with self.deps.uow() as work:
            work.account_id = account_id
            tx = work.tx
            role = await tx.roles.get(id=role_id, account_id=account_id)
            if role is None:
                msg = "role not found"
                raise NotFoundError(msg)
            if role.is_system:
                msg = "system roles cannot be changed"
                raise ForbiddenError(msg)
            details: dict[str, object] = {}
            if name is not None:
                cleaned = name.strip()
                if not 1 <= len(cleaned) <= 100:
                    msg = "role names have 1 to 100 characters"
                    raise InvalidInput(msg)
                details["name"] = {"from": role.name, "to": cleaned}
                role.name = cleaned
            if description is not None:
                role.description = description.strip()[:500]
            if permissions is not None:
                catalogue = await self.deps.catalogue.current(tx)
                keys = validate_role_permissions(permissions, catalogue)
                added = sorted(set(keys) - set(role.permissions))
                level = Level(LevelType.ACCOUNT, account_id)
                await self.deps.access.require_holds(tx, actor, account_id, added, level, catalogue)
                details["permissions"] = {"from": role.permissions, "to": keys}
                role.permissions = keys
                holders = await tx.assignments.find(role_id=role.id)
                work.touch(*{subject_of(a.subject_type, a.subject_id) for a in holders})
            role.updated_at = self.deps.clock()
            await tx.roles.update(role)
            await work.audit(
                ctx,
                "role.updated",
                account_id=account_id,
                target=("role", role.id),
                details=details,
            )
        return role

    async def delete_role(self, account_id: UUID, role_id: UUID, ctx: RequestContext) -> None:
        """Delete an unassigned, non-system role."""
        async with self.deps.uow() as work:
            role = await work.tx.roles.get(id=role_id, account_id=account_id)
            if role is None:
                msg = "role not found"
                raise NotFoundError(msg)
            if role.is_system:
                msg = "system roles cannot be deleted"
                raise ForbiddenError(msg)
            if await work.tx.assignments.count(role_id=role.id):
                msg = "the role is still assigned"
                raise ConflictError(msg)
            await work.tx.roles.delete(id=role.id)
            await work.audit(
                ctx,
                "role.deleted",
                account_id=account_id,
                target=("role", role.id),
                details={"key": role.key},
            )

    async def list_assignments(
        self, account_id: UUID, subject_id: UUID | None = None
    ) -> list[RoleAssignment]:
        """Assignments of the account (optionally of one subject)."""
        filters: dict[str, Any] = {"account_id": account_id}
        if subject_id is not None:
            filters["subject_id"] = subject_id
        async with self.deps.store.transaction() as tx:
            return await tx.assignments.find(order_by="created_at", **filters)

    async def assign(
        self,
        account_id: UUID,
        subject_type: SubjectType,
        subject_id: UUID,
        spec: AssignmentSpec,
        ctx: RequestContext,
    ) -> RoleAssignment:
        """Assign a role at a level (see :func:`assign_role`)."""
        async with self.deps.uow() as work:
            work.account_id = account_id
            catalogue = await self.deps.catalogue.current(work.tx)
            target = await work.tx.users.get(id=subject_id)
            if target is None:
                msg = "user not found"
                raise NotFoundError(msg)
            return await assign_role(
                self.deps, work, ctx, account_id, subject_type, subject_id, spec, catalogue
            )

    async def unassign(self, account_id: UUID, assignment_id: UUID, ctx: RequestContext) -> None:
        """Remove an assignment; the subject's tuples are reconciled."""
        actor = _actor(ctx)
        async with self.deps.uow() as work:
            work.account_id = account_id
            tx = work.tx
            assignment = await tx.assignments.get(id=assignment_id, account_id=account_id)
            if assignment is None:
                msg = "assignment not found"
                raise NotFoundError(msg)
            role = await tx.roles.get(id=assignment.role_id)
            if role is not None and role.kind is RoleKind.SYSTEM_ADMIN:
                actor_membership = await tx.memberships.get(
                    account_id=account_id, user_id=actor.subject
                )
                if actor_membership is None or not actor_membership.is_owner:
                    msg = "only the owner can revoke the admin role"
                    raise ForbiddenError(msg)
                membership = await tx.memberships.get(
                    account_id=account_id, user_id=assignment.subject_id
                )
                if membership is not None:
                    membership.is_admin = False
                    await tx.memberships.update(membership)
            await tx.assignments.delete(id=assignment.id)
            work.touch(subject_of(assignment.subject_type, assignment.subject_id))
            await work.audit(
                ctx,
                "role.unassigned",
                account_id=account_id,
                target=(assignment.subject_type.value, assignment.subject_id),
                details={
                    "role": role.key if role else str(assignment.role_id),
                    "level": str(assignment.level),
                },
            )
