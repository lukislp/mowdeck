"""``DriveChecker`` through ``mowdeck-authz``: ``can_drive`` on ``mower:<id>`` (fails closed)."""

from uuid import UUID

from mowdeck_authz import Authorizer, Relation, mower, user


class AuthzDriveChecker:
    """Implements the ``DriveChecker`` port with ``Authorizer.check``."""

    def __init__(self, authorizer: Authorizer) -> None:
        self._authorizer = authorizer

    async def can_drive(self, user_id: UUID, mower_id: UUID) -> bool:
        """True only when OpenFGA answers yes."""
        try:
            return await self._authorizer.check(user(user_id), Relation.CAN_DRIVE, mower(mower_id))
        except Exception:
            return False
