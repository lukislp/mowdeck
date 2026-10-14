"""Authorization questions the use cases ask themselves.

The HTTP layer guards the object named in the path (``mowdeck_authz.fastapi``). Checks that
depend on data are asked here, through the same :class:`~mowdeck_authz.Authorizer`: the target
site and groups of a registration or a move, every mower of a bulk command, and the filter of a
list. Answers fail closed (the authorizer never raises).
"""

from collections.abc import Sequence
from uuid import UUID

from mowdeck_authz import Authorizer, CheckItem, ObjectRef, Subject, account, principal_subject
from mowdeck_contracts.v1 import Principal

from fleet.domain.errors import ForbiddenError, NotFoundError
from fleet.domain.rules import CAN_VIEW


def subject_of(principal: Principal | None) -> Subject | None:
    """The OpenFGA subject of ``principal``; None for the system and service principals."""
    if principal is None:
        return None
    try:
        return principal_subject(principal)
    except ValueError:
        return None


class Access:
    """Object checks for one installation (wraps the authorizer)."""

    def __init__(self, authorizer: Authorizer) -> None:
        self.authorizer = authorizer

    async def require(
        self, principal: Principal | None, relation: str, obj: ObjectRef, *, hide: bool = True
    ) -> None:
        """Raise unless ``principal`` has ``relation`` on ``obj``.

        ``hide=True``: 403 (``ForbiddenError``) when the object is visible, else 404
        (``NotFoundError``), so a foreign object never reveals its existence. ``hide=False``:
        always 403 (account-wide checks, where existence is no secret).
        """
        subject = subject_of(principal)
        if subject is None:
            raise NotFoundError(f"{obj.type} not found") if hide else ForbiddenError("not allowed")
        if not hide:
            if not await self.authorizer.check(subject, relation, obj):
                raise ForbiddenError(f"missing {relation} on {obj.type}")
            return
        if relation == CAN_VIEW:
            allowed = visible = await self.authorizer.check(subject, relation, obj)
        else:
            allowed, visible = await self.authorizer.check_many(
                [CheckItem(subject, relation, obj), CheckItem(subject, CAN_VIEW, obj)]
            )
        if allowed:
            return
        if visible:
            raise ForbiddenError(f"missing {relation} on {obj.type}")
        raise NotFoundError(f"{obj.type} not found")

    async def classify(
        self, principal: Principal | None, relation: str, objects: Sequence[ObjectRef]
    ) -> list[tuple[bool, bool]]:
        """``(allowed, visible)`` for each object, in one batch."""
        subject = subject_of(principal)
        if subject is None or not objects:
            return [(False, False)] * len(objects)
        items = [CheckItem(subject, rel, obj) for obj in objects for rel in (relation, CAN_VIEW)]
        answers = await self.authorizer.check_many(items)
        return [(answers[2 * i], answers[2 * i + 1]) for i in range(len(objects))]

    async def visible_ids(
        self, principal: Principal | None, object_type: str, account_id: UUID
    ) -> set[UUID] | None:
        """Ids of ``object_type`` the principal may view; None means "all of the account".

        Short-circuit: ``can_view`` on the account covers every object below it, so the database
        filter alone answers the list. Otherwise ``list_objects`` gives the visible ids, which
        the caller intersects with the account's rows.
        """
        subject = subject_of(principal)
        if subject is None:
            return set()
        if await self.authorizer.check(subject, CAN_VIEW, account(account_id)):
            return None
        found: set[UUID] = set()
        for value in await self.authorizer.list_objects(subject, CAN_VIEW, object_type):
            try:
                found.add(UUID(ObjectRef.parse(value).id))
            except ValueError:
                continue
        return found
