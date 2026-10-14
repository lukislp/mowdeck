"""Authorization questions of the Planner (through ``mowdeck_authz.Authorizer``).

A preview or a coverage query needs ``can_view`` on the mower, a plan that is meant to be
started ``can_control`` (Fleet checks it again when the start command arrives). Answers fail
closed: an invisible mower is 404, a visible one without the relation 403.
"""

from mowdeck_authz import Authorizer, CheckItem, ObjectRef, principal_subject
from mowdeck_contracts.v1 import Principal

from planner.domain.errors import ForbiddenError, NotFoundError

CAN_VIEW = "can_view"
CAN_CONTROL = "can_control"


class Access:
    """Object checks for one installation (wraps the authorizer)."""

    def __init__(self, authorizer: Authorizer) -> None:
        self.authorizer = authorizer

    async def require(self, principal: Principal | None, relation: str, obj: ObjectRef) -> None:
        """Raise unless ``principal`` has ``relation`` on ``obj`` (403 visible, else 404)."""
        if principal is None:
            raise NotFoundError(f"{obj.type} not found")
        try:
            subject = principal_subject(principal)
        except ValueError:
            raise NotFoundError(f"{obj.type} not found") from None
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
        raise NotFoundError(f"{obj.type} not found")

    async def allowed(self, principal: Principal, relation: str, obj: ObjectRef) -> bool:
        """A plain check (False for principals without object rights)."""
        try:
            subject = principal_subject(principal)
        except ValueError:
            return False
        return await self.authorizer.check(subject, relation, obj)
