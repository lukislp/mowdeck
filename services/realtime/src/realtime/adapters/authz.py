"""``can_view`` and ``can_drive`` on mowers through ``mowdeck-authz`` (OpenFGA, cached for its
TTL)."""

from collections.abc import Sequence
from uuid import UUID

from mowdeck_authz import Authorizer, CheckItem, Relation, mower, principal_subject
from mowdeck_contracts.v1 import Principal


class AuthzAccessChecker:
    """The :class:`~realtime.application.ports.AccessChecker` over an :class:`Authorizer`."""

    def __init__(self, authorizer: Authorizer) -> None:
        self.authorizer = authorizer

    async def can_view(self, principal: Principal, mower_ids: Sequence[UUID]) -> list[bool]:
        """One batch (``check_many``); a principal without an OpenFGA subject sees nothing."""
        if not mower_ids:
            return []
        try:
            subject = principal_subject(principal)
        except ValueError:
            return [False] * len(mower_ids)
        return await self.authorizer.check_many(
            [CheckItem(subject, Relation.CAN_VIEW, mower(mower_id)) for mower_id in mower_ids]
        )

    async def can_drive(self, principal: Principal, mower_id: UUID) -> bool:
        """``can_drive`` on the mower; a principal without an OpenFGA subject may not drive."""
        try:
            subject = principal_subject(principal)
        except ValueError:
            return False
        return await self.authorizer.check(subject, Relation.CAN_DRIVE, mower(mower_id))

    async def available(self) -> bool:
        """``ping()`` of the authorizer when it has one, else True."""
        ping = getattr(self.authorizer, "ping", None)
        if ping is None:
            return True
        return bool(await ping())

    def subject_key(self, principal: Principal) -> str:
        """``user:<id>`` or ``api_token:<id>`` (the form of ``GrantsChanged.subjects``)."""
        try:
            return str(principal_subject(principal))
        except ValueError:
            return ""
