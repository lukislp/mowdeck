"""Authorization questions the use cases ask (through ``mowdeck_authz.Authorizer``).

Schedules are guarded by their target: ``can_view`` to see a schedule, ``can_edit_schedule``
(and ``can_control``, so the schedule can ever run) to create, change or delete it, and
``can_control`` for a manual run. Answers fail closed (the authorizer never raises).
"""

from collections.abc import Mapping
from typing import Any
from uuid import UUID

from mowdeck_authz import (
    Authorizer,
    CheckItem,
    ObjectRef,
    Subject,
    account,
    api_token,
    group,
    mower,
    principal_subject,
    user,
)
from mowdeck_contracts.v1 import Principal, PrincipalKind, PrincipalRef

from scheduler.domain.errors import ForbiddenError, NotFoundError
from scheduler.domain.rules import CAN_VIEW


def target_object(target_type: str, target_id: UUID) -> ObjectRef:
    """``mower:<id>`` or ``group:<id>``."""
    return mower(target_id) if target_type == "mower" else group(target_id)


def subject_of(principal: Principal | None) -> Subject | None:
    """The OpenFGA subject of ``principal``; None for service principals."""
    if principal is None:
        return None
    try:
        return principal_subject(principal)
    except ValueError:
        return None


def subject_of_ref(ref: PrincipalRef | Mapping[str, Any]) -> Subject | None:
    """The OpenFGA subject of a stored owner (``PrincipalRef`` or its JSON form)."""
    value = ref if isinstance(ref, PrincipalRef) else PrincipalRef.model_validate(ref)
    if value.kind == PrincipalKind.API_TOKEN:
        return api_token(value.subject)
    if value.kind in (PrincipalKind.USER, PrincipalKind.SUBUSER):
        return user(value.subject)
    return None


class Access:
    """Object checks for one installation (wraps the authorizer)."""

    def __init__(self, authorizer: Authorizer) -> None:
        self.authorizer = authorizer

    async def require(
        self, principal: Principal | None, relation: str, obj: ObjectRef, *, what: str = ""
    ) -> None:
        """Raise unless ``principal`` has ``relation`` on ``obj``: 403 (``ForbiddenError``) when
        the object is visible, else 404 (``NotFoundError``) naming ``what`` (default the type)."""
        name = what or obj.type
        subject = subject_of(principal)
        if subject is None:
            raise NotFoundError(f"{name} not found")
        if relation == CAN_VIEW:
            allowed = visible = await self.authorizer.check(subject, relation, obj)
        else:
            allowed, visible = await self.authorizer.check_many(
                [CheckItem(subject, relation, obj), CheckItem(subject, CAN_VIEW, obj)]
            )
        if allowed:
            return
        if visible:
            raise ForbiddenError(f"missing {relation} on the {obj.type}")
        raise NotFoundError(f"{name} not found")

    async def allowed(self, subject: Subject | None, relation: str, obj: ObjectRef) -> bool:
        """A plain check (False for no subject)."""
        if subject is None:
            return False
        return await self.authorizer.check(subject, relation, obj)

    async def visible_targets(
        self, principal: Principal | None, account_id: UUID
    ) -> set[str] | None:
        """``mower:<id>`` and ``group:<id>`` the principal may view; None means "all of the
        account" (``can_view`` on the account covers every object below it)."""
        subject = subject_of(principal)
        if subject is None:
            return set()
        if await self.authorizer.check(subject, CAN_VIEW, account(account_id)):
            return None
        found: set[str] = set()
        for object_type in ("mower", "group"):
            found.update(await self.authorizer.list_objects(subject, CAN_VIEW, object_type))
        return found
