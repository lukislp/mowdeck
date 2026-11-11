# SPDX-FileCopyrightText: 2026 Lukas Körber
# SPDX-License-Identifier: AGPL-3.0-only

"""Account erasure (ADR 0082): what Telemetry deletes when an account is deleted for good.

Identity asks on ``account.erase.telemetry`` once the grace period of a deleted account is
over, after Fleet removed the mowers and their last states have landed. The use case is the
kit's :class:`~mowdeck_service_kit.erasure.AccountErasure` over Telemetry's own parts:

* the purge is :class:`~telemetry.application.demo.DemoPurge`, the one of the demo sandboxes
  (ADR 0076), over all three stores: the mower directory, latest states, history, positions
  with their thinning watermarks, the command timeline, map and path snapshots, health totals
  and day rows, blade changes; virtual zones, their presence and day totals; theft tracking
  sessions with their links and photos. It also drops the account's aerial tile rate limit;
* ``drop_caches`` empties the in-process caches that are keyed by content, zone or session
  and so cannot be searched for one account (rendered widget maps, zone shapes, the map areas
  of tracking sessions). The demo purge leaves them to age out; an erased account's garden
  must not stay in memory, and the next request fills them again;
* what is left is counted in every store (``account_rows``). A row that is still there, or a
  purge that raised, is reported as a failure and Identity asks again.

Telemetry writes no OpenFGA tuples. The aerial tile cache on disk holds the provider's tiles
by URL and nothing of an account.
"""

from collections.abc import Callable, Sequence
from uuid import UUID

from mowdeck_service_kit.erasure import AccountErasure

from telemetry.application.demo import DemoPurge
from telemetry.application.ports import Store
from telemetry.application.tracking_ports import TrackingStore
from telemetry.application.zone_ports import ZoneStore


def account_erasure(
    purge: DemoPurge,
    store: Store,
    zones: ZoneStore,
    tracking: TrackingStore,
    drop_caches: Sequence[Callable[[], None]] = (),
) -> AccountErasure:
    """The erasure of one account in Telemetry."""

    async def leftovers(account_id: UUID) -> dict[str, int]:
        return {
            **await store.account_rows(account_id),
            **await zones.account_rows(account_id),
            **await tracking.account_rows(account_id),
        }

    def forget(account_id: UUID) -> None:
        del account_id  # these caches hold no account key: all of them go
        for drop in drop_caches:
            drop()

    return AccountErasure(purge.purge_account, leftovers, forget=[forget])
