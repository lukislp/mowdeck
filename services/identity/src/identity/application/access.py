"""Who holds what: token claims of users and API tokens, and the anti-escalation checks.

Anti-escalation (design section 3.6): an actor can only hand out what it holds itself.

* Model keys are checked in OpenFGA as the computed relation on the target level (so an
  Operator on group North cannot assign Manager there, and nobody can grant on another
  account's objects).
* Account keys are checked from the database on the whole account.
* Only the owner assigns or revokes Admin; nobody assigns Owner.
"""

from collections.abc import Iterable
from datetime import datetime
from uuid import UUID

from mowdeck_authz import PERMISSION_RELATIONS, Authorizer, CheckItem, user
from mowdeck_contracts.v1 import Principal, PrincipalKind

from identity.application.ports import Tx
from identity.application.sync import AuthzSync, level_object
from identity.domain.errors import ForbiddenError, NotFoundError
from identity.domain.models import (
    Account,
    ApiToken,
    Level,
    LevelType,
    Membership,
    SubjectType,
    User,
    UserKind,
)
from identity.domain.permissions import Catalogue, HeldRole, held_on_account, token_permissions


async def held_roles(tx: Tx, account_id: UUID, user_id: UUID) -> list[HeldRole]:
    """Roles a user holds in an account, with their levels."""
    held: list[HeldRole] = []
    for assignment in await tx.assignments.find(
        account_id=account_id, subject_type=SubjectType.USER.value, subject_id=user_id
    ):
        role = await tx.roles.get(id=assignment.role_id)
        if role is not None:
            held.append(HeldRole(role, assignment.level_type))
    return held


async def user_principal(
    tx: Tx,
    found: User,
    acct: Account,
    membership: Membership,
    catalogue: Catalogue,
    *,
    session_id: UUID | None,
    auth_time: datetime | None = None,
    auth_method: str | None = None,
) -> Principal:
    """The principal (token claims) of a user acting in an account."""
    roles, perms = token_permissions(membership, await held_roles(tx, acct.id, found.id), catalogue)
    return Principal(
        subject=found.id,
        account_id=acct.id,
        kind=PrincipalKind.SUBUSER if found.kind is UserKind.SUBUSER else PrincipalKind.USER,
        display_name=found.display_name,
        roles=roles,
        permissions=perms,
        plan=acct.plan,
        session_id=session_id,
        auth_time=auth_time,
        auth_methods=[auth_method] if auth_time is not None and auth_method else [],
    )


def token_principal(token: ApiToken, acct: Account, catalogue: Catalogue) -> Principal:
    """The principal of an API token: only its own grants (plus implied account keys)."""
    keys: set[str] = set()
    on_account: set[str] = set()
    for grant in token.token_grants:
        effective = catalogue.effective(grant.permissions, grant.level.type, SubjectType.API_TOKEN)
        keys.update(effective)
        if grant.level.type is LevelType.ACCOUNT:
            on_account.update(effective)
    keys |= catalogue.implied_account_keys(on_account)
    return Principal(
        subject=token.id,
        account_id=acct.id,
        kind=PrincipalKind.API_TOKEN,
        display_name=token.name,
        roles=[],
        permissions=sorted(keys),
        plan=acct.plan,
    )


class AccessChecker:
    """Answers "may this actor hand out these keys on this level?"."""

    def __init__(self, authorizer: Authorizer, sync: AuthzSync) -> None:
        self.authorizer = authorizer
        self.sync = sync

    async def require_level_in_account(self, level: Level, account_id: UUID) -> None:
        """404 unless the level belongs to the account (Fleet's ``account`` tuple)."""
        if level.type is LevelType.ACCOUNT:
            if level.id != account_id:
                msg = "level not found"
                raise NotFoundError(msg)
            return
        owner = await self.sync.account_of(level_object(level.type, level.id))
        if owner != account_id:
            msg = f"{level.type.value} not found"
            raise NotFoundError(msg)

    async def require_holds(
        self,
        tx: Tx,
        actor: Principal,
        account_id: UUID,
        keys: Iterable[str],
        level: Level,
        catalogue: Catalogue,
    ) -> None:
        """403 unless ``actor`` holds every key in ``keys`` on ``level``."""
        wanted = sorted(set(keys))
        if not wanted:
            return
        if actor.kind not in (PrincipalKind.USER, PrincipalKind.SUBUSER):
            msg = "only users can hand out permissions"
            raise ForbiddenError(msg)
        model_keys = [k for k in wanted if catalogue.entry(k).in_model]
        account_keys = [k for k in wanted if not catalogue.entry(k).in_model]
        if account_keys:
            membership = await tx.memberships.get(account_id=account_id, user_id=actor.subject)
            held = held_on_account(
                membership, await held_roles(tx, account_id, actor.subject), catalogue
            )
            missing = [k for k in account_keys if k not in held]
            if missing:
                msg = f"you do not hold {', '.join(missing)} on the account"
                raise ForbiddenError(msg)
        if model_keys:
            obj = level_object(level.type, level.id)
            subject = user(actor.subject)
            answers = await self.authorizer.check_many(
                [CheckItem(subject, PERMISSION_RELATIONS[k].computed, obj) for k in model_keys]
            )
            missing = [k for k, ok in zip(model_keys, answers, strict=True) if not ok]
            if missing:
                msg = f"you do not hold {', '.join(missing)} on {level.type.value}"
                raise ForbiddenError(msg)
