"""Recipient checks through ``mowdeck-authz`` (one batch per event): ``can_view``, or
``can_drive`` for kinds that ask the recipient to act on the mower (``vision_proposal``)."""

from collections.abc import Sequence
from uuid import UUID

from mowdeck_authz import Authorizer, CheckItem, Relation, mower, user


class AuthzViewChecker:
    """Implements the ``ViewChecker`` port with ``Authorizer.check_many`` (fails closed)."""

    def __init__(self, authorizer: Authorizer) -> None:
        self._authorizer = authorizer

    async def can_view(
        self, user_ids: Sequence[UUID], mower_id: UUID, relation: str = Relation.CAN_VIEW
    ) -> list[bool]:
        """``relation`` (default ``can_view``) on ``mower:<id>`` for every user, in order."""
        if not user_ids:
            return []
        items = [CheckItem(user(uid), relation, mower(mower_id)) for uid in user_ids]
        try:
            return list(await self._authorizer.check_many(items))
        except Exception:
            return [False] * len(user_ids)
