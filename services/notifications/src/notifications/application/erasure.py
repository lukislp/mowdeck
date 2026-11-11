# SPDX-FileCopyrightText: 2026 Lukas Körber
# SPDX-License-Identifier: AGPL-3.0-only

"""Account erasure (ADR 0082): what Notifications deletes when an account is deleted for good.

Identity asks on ``account.erase.notifications`` once the grace period of a deleted account is
over, after Fleet removed the account's mowers (``mower.removed`` already made this service drop
their directory entry, watch and geofence and what it kept of them in memory).

The use case is the kit's :class:`~mowdeck_service_kit.erasure.AccountErasure` over the parts
Notifications owns:

* the purge is :class:`~notifications.application.demo.DemoPurge`, the one of the demo sandboxes
  (ADR 0076): one transaction over every table of the account (preferences, test throttle, Web
  Push subscriptions, APNs devices, the ntfy target, inbox, camera photos, queued and finished
  deliveries, alarm silences, Live Activity devices and activities, mower watch, geofence and
  directory), then the per-mower memory of the mowers those rows named;
* afterwards the work this replica already started for the account is cancelled: the deliveries
  the outbox claimed before the purge (a queued message must not leave for an erased account)
  and the camera photos still being taken (a photo that arrived later would be stored as a row
  of the account again);
* what is left is counted over every table with an ``account_id`` column
  (:meth:`notifications.application.ports.Store.account_rows`). A row that is still there, or a
  purge that raised, is reported as a failure; Identity asks again.

Nothing is sent to a push service as part of an erasure: the tokens, endpoints and the ntfy
target are deleted here, which is what stops the pushes (ADR 0082, "Third parties").
"""

from mowdeck_service_kit.erasure import AccountErasure

from notifications.application.demo import DemoPurge
from notifications.application.fanout import Fanout
from notifications.application.outbox import Outbox
from notifications.application.ports import Store


def account_erasure(
    purge: DemoPurge, store: Store, outbox: Outbox, fanout: Fanout
) -> AccountErasure:
    """The erasure of one account in Notifications."""
    return AccountErasure(
        purge.purge_account,
        store.account_rows,
        forget=[outbox.forget_account, fanout.forget_account],
    )
