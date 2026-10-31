"""Account administration under ``/api/v1/identity/accounts/{account_id}``.

The account in the path must be the account of the token (else 404), and every endpoint asks
OpenFGA for the relation in the table of the README (``require_or_not_found`` with ``member``
as the visibility relation: members without the right get 403, everybody else 404).
"""

from datetime import datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Query
from mowdeck_authz import Relation, account, user
from mowdeck_service_kit.errors import Forbidden

from identity.adapters.api.deps import (
    Ctx,
    FreshAuth,
    ManageAccount,
    ManageUsers,
    Member,
    ServicesDep,
    ViewAudit,
)
from identity.adapters.api.schemas import (
    AccountDeleteIn,
    AccountOut,
    AccountPatch,
    ApiTokenIn,
    ApiTokenOut,
    ApiTokensPurgedOut,
    AuditOut,
    MemberOut,
    PasskeyOut,
    PasswordSetIn,
    PermissionOut,
    ReconcileOut,
    RoleAssignmentIn,
    RoleAssignmentOut,
    RoleIn,
    RoleOut,
    RolePatch,
    SubUserIn,
    UserPatch,
)
from identity.application.audit import AuditQuery
from identity.application.roles import AssignmentSpec
from identity.application.users import NewSubUser
from identity.domain.models import SubjectType, TokenGrant

router = APIRouter(prefix="/identity/accounts/{account_id}", tags=["identity: accounts"])


# --- account ------------------------------------------------------------------------------------


@router.get("")
async def get_account(account_id: UUID, _: Member, services: ServicesDep) -> AccountOut:
    """The account (any member)."""
    return AccountOut.of(await services.accounts.get(account_id))


@router.patch("")
async def rename_account(
    account_id: UUID, body: AccountPatch, _: ManageUsers, services: ServicesDep, ctx: Ctx
) -> AccountOut:
    """Rename the account (``can_manage_users``)."""
    return AccountOut.of(await services.accounts.rename(account_id, body.name, ctx))


@router.delete("", status_code=202)
async def delete_account(
    account_id: UUID,
    _: ManageAccount,
    services: ServicesDep,
    ctx: Ctx,
    body: AccountDeleteIn | None = None,
) -> AccountOut:
    """Request deletion (owner): locked for the grace period. Needs the password in the body
    or, without it, a recent step-up confirmation."""
    password = body.password if body else None
    return AccountOut.of(await services.accounts.request_deletion(account_id, password, ctx))


@router.post("/restore")
async def restore_account(
    account_id: UUID, _: ManageAccount, services: ServicesDep, ctx: Ctx
) -> AccountOut:
    """Undo a deletion request within the grace period (owner)."""
    return AccountOut.of(await services.accounts.restore(account_id, ctx))


@router.post("/reconcile")
async def reconcile(
    account_id: UUID, _: ManageUsers, services: ServicesDep, ctx: Ctx
) -> ReconcileOut:
    """Repair OpenFGA from the database for every subject of the account."""
    result = await services.accounts.reconcile_account(account_id, ctx)
    return ReconcileOut(
        writes=result.writes,
        deletes=result.deletes,
        changed_subjects=sorted(result.changed_subjects),
    )


@router.get("/permissions")
async def permissions(account_id: UUID, _: Member, services: ServicesDep) -> list[PermissionOut]:
    """The permission catalogue (what roles and tokens can contain)."""
    catalogue = await services.deps.catalogue.current()
    return [PermissionOut.of(e) for e in catalogue.entries]


# --- users --------------------------------------------------------------------------------------


@router.get("/users")
async def list_users(account_id: UUID, _: ManageUsers, services: ServicesDep) -> list[MemberOut]:
    """Members and sub-users."""
    return [MemberOut.of(v) for v in await services.users.list_members(account_id)]


@router.post("/users", status_code=201)
async def create_user(
    account_id: UUID,
    body: SubUserIn,
    _: ManageUsers,
    __: FreshAuth,
    services: ServicesDep,
    ctx: Ctx,
) -> MemberOut:
    """Create a sub-user, optionally with roles at levels (anti-escalation applies). Step-up."""
    view = await services.users.create_subuser(
        account_id,
        NewSubUser(
            username=body.username,
            password=body.password,
            display_name=body.display_name,
            email=body.email,
            must_change_password=body.must_change_password,
            assignments=[AssignmentSpec(a.role_id, a.level.level()) for a in body.assignments],
        ),
        ctx,
    )
    return MemberOut.of(view)


@router.get("/users/{user_id}")
async def get_user(
    account_id: UUID, user_id: UUID, principal: Member, services: ServicesDep
) -> MemberOut:
    """A member (yourself, or anybody with ``can_manage_users``)."""
    if principal.subject != user_id and not await services.deps.authorizer.check(
        user(principal.subject), Relation.CAN_MANAGE_USERS, account(account_id)
    ):
        raise Forbidden("missing can_manage_users on account")
    return MemberOut.of(await services.users.get_member(account_id, user_id))


@router.patch("/users/{user_id}")
async def rename_user(
    account_id: UUID,
    user_id: UUID,
    body: UserPatch,
    _: ManageUsers,
    services: ServicesDep,
    ctx: Ctx,
) -> MemberOut:
    """Change the display name of a sub-user."""
    return MemberOut.of(await services.users.rename(account_id, user_id, body.display_name, ctx))


@router.post("/users/{user_id}/disable")
async def disable_user(
    account_id: UUID, user_id: UUID, _: ManageUsers, services: ServicesDep, ctx: Ctx
) -> MemberOut:
    """Disable: sessions end, the user's API tokens are revoked, tuples are removed."""
    return MemberOut.of(await services.users.set_disabled(account_id, user_id, True, ctx))


@router.post("/users/{user_id}/enable")
async def enable_user(
    account_id: UUID, user_id: UUID, _: ManageUsers, services: ServicesDep, ctx: Ctx
) -> MemberOut:
    """Enable again (revoked API tokens stay revoked)."""
    return MemberOut.of(await services.users.set_disabled(account_id, user_id, False, ctx))


@router.post("/users/{user_id}/password", status_code=204)
async def set_password(
    account_id: UUID,
    user_id: UUID,
    body: PasswordSetIn,
    _: ManageUsers,
    __: FreshAuth,
    services: ServicesDep,
    ctx: Ctx,
) -> None:
    """Set a temporary password of a sub-user (must be changed at the next login). Step-up."""
    await services.users.set_password(account_id, user_id, body.password, ctx)


@router.delete("/users/{user_id}", status_code=204)
async def delete_user(
    account_id: UUID, user_id: UUID, _: ManageUsers, __: FreshAuth, services: ServicesDep, ctx: Ctx
) -> None:
    """Delete a sub-user, or remove a person from the account. Step-up."""
    await services.users.remove(account_id, user_id, ctx)


@router.get("/users/{user_id}/passkeys")
async def list_user_passkeys(
    account_id: UUID, user_id: UUID, _: ManageUsers, services: ServicesDep
) -> list[PasskeyOut]:
    """Passkeys of a sub-user of this account (persons manage their own: 403)."""
    return [PasskeyOut.of(p) for p in await services.passkeys.list_of_user(account_id, user_id)]


@router.delete("/users/{user_id}/passkeys/{passkey_id}", status_code=204)
async def remove_user_passkey(
    account_id: UUID,
    user_id: UUID,
    passkey_id: UUID,
    _: ManageUsers,
    __: FreshAuth,
    services: ServicesDep,
    ctx: Ctx,
) -> None:
    """Remove a passkey of a sub-user of this account (its password stays). Step-up."""
    await services.passkeys.remove_of_user(ctx, account_id, user_id, passkey_id)


# --- roles and assignments ----------------------------------------------------------------------


@router.get("/roles")
async def list_roles(account_id: UUID, _: Member, services: ServicesDep) -> list[RoleOut]:
    """Roles of the account."""
    return [RoleOut.of(r) for r in await services.roles.list_roles(account_id)]


@router.post("/roles", status_code=201)
async def create_role(
    account_id: UUID, body: RoleIn, _: ManageUsers, services: ServicesDep, ctx: Ctx
) -> RoleOut:
    """A custom role (never more than the actor holds)."""
    return RoleOut.of(
        await services.roles.create_role(
            account_id, body.name, body.permissions, body.description, ctx
        )
    )


@router.patch("/roles/{role_id}")
async def update_role(
    account_id: UUID,
    role_id: UUID,
    body: RolePatch,
    _: ManageUsers,
    __: FreshAuth,
    services: ServicesDep,
    ctx: Ctx,
) -> RoleOut:
    """Change a role; every holder is reconciled. Step-up."""
    role = await services.roles.update_role(
        account_id,
        role_id,
        ctx,
        name=body.name,
        permissions=body.permissions,
        description=body.description,
    )
    return RoleOut.of(role)


@router.delete("/roles/{role_id}", status_code=204)
async def delete_role(
    account_id: UUID, role_id: UUID, _: ManageUsers, services: ServicesDep, ctx: Ctx
) -> None:
    """Delete an unassigned custom or default role."""
    await services.roles.delete_role(account_id, role_id, ctx)


@router.get("/role-assignments")
async def list_assignments(
    account_id: UUID,
    principal: Member,
    services: ServicesDep,
    subject_id: Annotated[UUID | None, Query()] = None,
) -> list[RoleAssignmentOut]:
    """Assignments of the account (own ones for members without ``can_manage_users``)."""
    if subject_id != principal.subject and not await services.deps.authorizer.check(
        user(principal.subject), Relation.CAN_MANAGE_USERS, account(account_id)
    ):
        raise Forbidden("missing can_manage_users on account")
    return [
        RoleAssignmentOut.of(a)
        for a in await services.roles.list_assignments(account_id, subject_id)
    ]


@router.post("/role-assignments", status_code=201)
async def assign_role(
    account_id: UUID,
    body: RoleAssignmentIn,
    _: ManageUsers,
    __: FreshAuth,
    services: ServicesDep,
    ctx: Ctx,
) -> RoleAssignmentOut:
    """Assign a role at a level; writes the expanded grant tuples. Step-up."""
    assignment = await services.roles.assign(
        account_id,
        SubjectType(body.subject_type),
        body.subject_id,
        AssignmentSpec(body.role_id, body.level.level()),
        ctx,
    )
    return RoleAssignmentOut.of(assignment)


@router.delete("/role-assignments/{assignment_id}", status_code=204)
async def unassign_role(
    account_id: UUID,
    assignment_id: UUID,
    _: ManageUsers,
    __: FreshAuth,
    services: ServicesDep,
    ctx: Ctx,
) -> None:
    """Remove an assignment; the subject's tuples are reconciled. Step-up."""
    await services.roles.unassign(account_id, assignment_id, ctx)


# --- API tokens -----------------------------------------------------------------------------------


@router.get("/api-tokens")
async def list_tokens(account_id: UUID, _: ManageUsers, services: ServicesDep) -> list[ApiTokenOut]:
    """API tokens of the account (without secrets)."""
    return [ApiTokenOut.of(t) for t in await services.tokens.list_tokens(account_id)]


@router.post("/api-tokens", status_code=201)
async def create_token(
    account_id: UUID,
    body: ApiTokenIn,
    _: ManageUsers,
    __: FreshAuth,
    services: ServicesDep,
    ctx: Ctx,
) -> ApiTokenOut:
    """Create an API token; the secret is in the response once. Step-up."""
    created = await services.tokens.create(
        account_id,
        body.name,
        [TokenGrant(g.level.level(), tuple(g.permissions)) for g in body.grants],
        body.expires_at,
        ctx,
    )
    return ApiTokenOut.of(created.token, created.secret)


@router.post("/api-tokens/purge-revoked")
async def purge_revoked_tokens(
    account_id: UUID, _: ManageUsers, services: ServicesDep, ctx: Ctx
) -> ApiTokensPurgedOut:
    """Delete every revoked API token of the account for good; active tokens stay. Each one is
    audited (``api_token.purged``); the audit history of the tokens stays."""
    purged = await services.tokens.purge_revoked(account_id, ctx)
    return ApiTokensPurgedOut(deleted=len(purged), ids=[token.id for token in purged])


@router.delete("/api-tokens/{token_id}", status_code=204)
async def revoke_token(
    account_id: UUID,
    token_id: UUID,
    _: ManageUsers,
    services: ServicesDep,
    ctx: Ctx,
    purge: Annotated[
        bool,
        Query(
            description=(
                "Delete the revoked token for good instead of revoking it; "
                "409 while the token is not revoked"
            )
        ),
    ] = False,
) -> None:
    """Revoke an API token; with ``purge=true`` delete an already revoked one for good."""
    if purge:
        await services.tokens.purge(account_id, token_id, ctx)
    else:
        await services.tokens.revoke(account_id, token_id, ctx)


# --- audit ----------------------------------------------------------------------------------------


@router.get("/audit")
async def audit(
    account_id: UUID,
    principal: ViewAudit,
    services: ServicesDep,
    action: Annotated[str | None, Query(max_length=64)] = None,
    actor: Annotated[str | None, Query(max_length=64)] = None,
    target_id: Annotated[str | None, Query(max_length=64)] = None,
    since: Annotated[datetime | None, Query()] = None,
    before_id: Annotated[int | None, Query(ge=1)] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> list[AuditOut]:
    """Audit log of the account, newest first; page with ``before_id``."""
    # The public demo (ADR 0068) shows only the caller's own login session, without addresses.
    entries = await services.audit.query(
        account_id,
        AuditQuery(
            action=action,
            actor=actor,
            target_id=target_id,
            since=since,
            before_id=before_id,
            limit=limit,
        ),
        principal,
    )
    return [AuditOut.of(e) for e in entries]
