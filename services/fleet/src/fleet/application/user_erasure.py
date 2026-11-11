# SPDX-FileCopyrightText: 2026 Lukas Körber
# SPDX-License-Identifier: AGPL-3.0-only

"""A deleted user (ADR 0082, "delete me"): what Fleet forgets of a person.

Identity publishes ``identity.user.deleted`` when a member deletes themselves or an admin deletes
a sub-user. The account goes on and so does everything in it: the sites, groups and mowers the
user created, the commands they sent and their drive sessions are the account's record and stay.
What goes is the person's name, which Fleet copied into two places as part of a
``PrincipalRef``: who issued a command (``commands.issued_by``) and who started a drive session
(``drive_sessions.started_by``). The reference keeps its opaque subject id, like the plain
``created_by``/``updated_by`` ids and the audit log's ``actor_subject`` do; once Identity's user
row is gone they name nobody.

Fleet keeps no row that is a user's own (no table has a ``user_id`` column; a test holds that
against the schema), and nothing of a user in memory.

The use case is the kit's :class:`~mowdeck_service_kit.user_erasure.UserErasure` over Fleet's
store: the scrub and the count of what is left both read the reference columns from the schema
(:meth:`fleet.application.ports.Tx.forget_user`, :meth:`~fleet.application.ports.Tx.user_names`).
"""

from uuid import UUID

from mowdeck_service_kit.user_erasure import UserErasure

from fleet.application.ports import Store


def user_erasure(store: Store) -> UserErasure:
    """The erasure of one user in Fleet."""

    async def forget(user_id: UUID, account_id: UUID | None) -> int:
        async with store.transaction() as tx:
            return await tx.forget_user(user_id, account_id)

    async def leftovers(user_id: UUID) -> dict[str, int]:
        async with store.transaction() as tx:
            return await tx.user_names(user_id)

    return UserErasure(forget, leftovers)
