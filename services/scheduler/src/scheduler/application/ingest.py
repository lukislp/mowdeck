# SPDX-FileCopyrightText: 2026 Lukas Körber
# SPDX-License-Identifier: AGPL-3.0-only

"""Facts about mowers from the bus: the latest activity (busy check, last position), the map
(geo origin for the weather, area ids for "all areas") and the capabilities (what a schedule
may set for the mower).

The status is what the runner reads when it fires and follows up: the activity (exact) and a
position for the weather (coarse). A mowing mower reports every second or so, and writing each
report was most of the Scheduler's work per state (docs/ops/load-testing.md, "Ingestion
cost"), so a replica skips a state whose activity equals the one it last wrote for the mower,
whose position moved less than :data:`STATUS_MOVE_M` and that is less than
:data:`STATUS_REFRESH` newer. A changed activity is always written at once; the stored
``observed_at`` and position lag the stream by at most :data:`STATUS_REFRESH` otherwise.
"""

import math
from datetime import timedelta
from uuid import UUID

from mowdeck_contracts.v1 import MowerCapabilitiesSnapshot, MowerMapSnapshot, MowerState

from scheduler.application.ports import Store
from scheduler.domain.models import MowerPlace, MowerStatus

STATUS_REFRESH = timedelta(seconds=30)
"""An unchanged status is written again at least this often (the follow-up of a started run
compares ``observed_at`` with the start; a few seconds of lag do not matter there)."""

STATUS_MOVE_M = 100.0
"""A position change of at least this many metres is written at once (the weather needs the
place, not the spot in the garden)."""

_EARTH_RADIUS_M = 6_371_000.0
_MAX_REMEMBERED = 4096


def _distance_m(a: MowerStatus, b: MowerStatus) -> float:
    """Distance of two positions (equirectangular; exact enough for the threshold)."""
    if a.latitude is None or a.longitude is None or b.latitude is None or b.longitude is None:
        return 0.0
    lat = math.radians((a.latitude + b.latitude) / 2)
    dx = math.radians(b.longitude - a.longitude) * math.cos(lat)
    dy = math.radians(b.latitude - a.latitude)
    return _EARTH_RADIUS_M * math.hypot(dx, dy)


def status_due(written: MowerStatus | None, status: MowerStatus) -> bool:
    """Whether ``status`` must be written, given the one this replica wrote last (None: none
    or unknown)."""
    if written is None or written.account_id != status.account_id:
        return True
    if status.observed_at <= written.observed_at:
        return False  # older or the same report: the store would keep the stored one anyway
    if status.activity != written.activity:
        return True
    if (status.latitude is None) != (written.latitude is None):
        return True
    if status.observed_at - written.observed_at >= STATUS_REFRESH:
        return True
    return _distance_m(written, status) >= STATUS_MOVE_M


class Ingest:
    """Keeps the per-mower facts the runner reads."""

    def __init__(self, store: Store) -> None:
        self.store = store
        self._written: dict[UUID, MowerStatus] = {}
        """The status this replica last wrote per mower (see the module documentation)."""

    async def on_state(self, state: MowerState) -> bool:
        """Store the activity unless a newer one is stored or nothing relevant changed since
        this replica wrote the mower's status; True if stored."""
        position = state.position
        status = MowerStatus(
            mower_id=state.mower_id,
            account_id=state.account_id,
            activity=state.activity.value,
            observed_at=state.observed_at,
            latitude=position.lat if position else None,
            longitude=position.lon if position else None,
        )
        if not status_due(self._written.get(state.mower_id), status):
            return False
        async with self.store.transaction() as tx:
            stored = await tx.put_status(status)
        if len(self._written) >= _MAX_REMEMBERED:
            self._written.clear()
        self._written[state.mower_id] = status
        return stored

    async def on_capabilities(self, snapshot: MowerCapabilitiesSnapshot) -> bool:
        """Store the capabilities unless newer ones are stored; True if stored."""
        async with self.store.transaction() as tx:
            return await tx.put_capabilities(
                snapshot.mower_id,
                snapshot.account_id,
                snapshot.observed_at,
                snapshot.capabilities,
            )

    async def on_map(self, snapshot: MowerMapSnapshot) -> None:
        """Store the map's geo origin and area ids."""
        reference = snapshot.map.reference
        # (0, 0) is the placeholder of maps without a geo reference, not a real place.
        known = (reference.origin_lat, reference.origin_lon) != (0.0, 0.0)
        async with self.store.transaction() as tx:
            await tx.put_place(
                MowerPlace(
                    mower_id=snapshot.mower_id,
                    account_id=snapshot.account_id,
                    observed_at=snapshot.observed_at,
                    latitude=reference.origin_lat if known else None,
                    longitude=reference.origin_lon if known else None,
                    area_ids=snapshot.map.area_ids,
                )
            )
