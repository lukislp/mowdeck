# SPDX-FileCopyrightText: 2026 Lukas Körber
# SPDX-License-Identifier: AGPL-3.0-only

"""A deleted user (ADR 0082, "delete me"): what Telemetry takes back from a person.

Identity publishes ``identity.user.deleted`` when a member deletes themselves or an admin deletes
a sub-user. Telemetry stores no name and no row that is a user's own: its ``changed_by``,
``started_by`` and ``created_by`` columns are plain subject ids (a test holds against the schema
that no column stores a principal reference and no table has a ``user_id`` column), and an id
names nobody once Identity's user row is gone. The blade changes, zones and tracking sessions the
user created are the account's and stay.

One thing a person still *holds* after they are gone: the secret share links of a theft
tracking session they created (ADR 0058). A link shows the mower's position to whoever has its
token, without signing in, until it expires. So the links the deleted user created are revoked,
as Identity revokes the API tokens a removed user created. The session itself goes on and the
other members create new links.

The use case is the kit's :class:`~mowdeck_service_kit.user_erasure.UserErasure` over the
tracking store; what is left is the count of the user's links that still work.
"""

from uuid import UUID

from mowdeck_service_kit.user_erasure import UserErasure

from telemetry.application.ports import Clock
from telemetry.application.tracking_ports import TrackingStore


def user_erasure(tracking: TrackingStore, clock: Clock) -> UserErasure:
    """The erasure of one user in Telemetry."""

    async def forget(user_id: UUID, account_id: UUID | None) -> int:
        return await tracking.revoke_links_of_creator(user_id, account_id, clock())

    async def leftovers(user_id: UUID) -> dict[str, int]:
        usable = await tracking.usable_links_of_creator(user_id, clock())
        return {"tracking_links": usable} if usable else {}

    return UserErasure(forget, leftovers)
