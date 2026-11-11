# SPDX-FileCopyrightText: 2026 Lukas Körber
# SPDX-License-Identifier: AGPL-3.0-only

"""A deleted user (ADR 0082, "delete me"): what the Scheduler forgets of a person.

Identity publishes ``identity.user.deleted`` when a member deletes themselves or an admin deletes
a sub-user. The account goes on: the schedules the user created and the runs they triggered are
the account's and stay. What goes is the person's name, which the Scheduler copied into two
places as part of a ``PrincipalRef``: the owner of a schedule (``schedules.owner``) and the
subject a run was issued for (``schedule_runs.issued_by``). The reference keeps its opaque
subject id (``owner_subject`` too), which names nobody once Identity's user row is gone.

A schedule keeps its owner's id on purpose: every firing re-checks that the owner may still
control the target (:mod:`scheduler.application.runner`). A deleted user holds no permission, so
the schedule skips its runs and, after the configured number of skips, is disabled with the
reason shown in the app, exactly as for an owner who lost the right. Whoever edits or re-enables
it becomes its owner. Nothing mows on behalf of somebody who is gone.

The Scheduler keeps no row that is a user's own (no table has a ``user_id`` column; a test holds
that against the schema), and nothing of a user in memory.

The use case is the kit's :class:`~mowdeck_service_kit.user_erasure.UserErasure` over the
Scheduler's store: the scrub and the count of what is left both read the reference columns from
the schema (:meth:`scheduler.application.ports.Tx.forget_user`,
:meth:`~scheduler.application.ports.Tx.user_names`).
"""

from uuid import UUID

from mowdeck_service_kit.user_erasure import UserErasure

from scheduler.application.ports import Store


def user_erasure(store: Store) -> UserErasure:
    """The erasure of one user in the Scheduler."""

    async def forget(user_id: UUID, account_id: UUID | None) -> int:
        async with store.transaction() as tx:
            return await tx.forget_user(user_id, account_id)

    async def leftovers(user_id: UUID) -> dict[str, int]:
        async with store.transaction() as tx:
            return await tx.user_names(user_id)

    return UserErasure(forget, leftovers)
