# SPDX-FileCopyrightText: 2026 Lukas Körber
# SPDX-License-Identifier: AGPL-3.0-only

"""A deleted user (ADR 0082, "delete me"): what Notifications forgets of a person.

Identity publishes ``identity.user.deleted`` when a member deletes themselves or an admin deletes
a sub-user. The account goes on, and so do its mower directory, watches, geofences and the
maintenance window. What goes is everything that is the person's: every row with a ``user_id``
column, read from the schema (preferences, the test throttle, Web Push subscriptions, APNs
devices, the ntfy target, the inbox, queued and finished deliveries, alarm silences, Live
Activity devices and activities), and the camera photos that only the person's inbox items
showed (a photo is one row per event, shared by the items of every recipient; a photo another
member's item still shows stays with that item). Notifications stores no principal reference,
so no name has to be scrubbed from rows that stay (a test holds that against the schema).

In memory, the outbox cancels the sends to the person that this replica had already claimed: the
rows are gone, and a queued message must not reach the person's devices afterwards. A send that
already reached the channel's server cannot be taken back.

The use case is the kit's :class:`~mowdeck_service_kit.user_erasure.UserErasure`: the store's
``forget_user``, the outbox, then a count of the user's rows over the same tables
(:meth:`notifications.application.ports.Store.user_rows`). A row that is still there fails the
event, which comes again.
"""

from mowdeck_service_kit.user_erasure import UserErasure

from notifications.application.outbox import Outbox
from notifications.application.ports import Store


def user_erasure(store: Store, outbox: Outbox) -> UserErasure:
    """The erasure of one user in Notifications."""
    return UserErasure(store.forget_user, store.user_rows, in_memory=[outbox.forget_user])
