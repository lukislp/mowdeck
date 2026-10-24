"""Facts about mowers from the bus: the latest activity (busy check, last position), the map
(geo origin for the weather, area ids for "all areas") and the capabilities (what a schedule
may set for the mower)."""

from mowdeck_contracts.v1 import MowerCapabilitiesSnapshot, MowerMapSnapshot, MowerState

from scheduler.application.ports import Store
from scheduler.domain.models import MowerPlace, MowerStatus


class Ingest:
    """Keeps the per-mower facts the runner reads."""

    def __init__(self, store: Store) -> None:
        self.store = store

    async def on_state(self, state: MowerState) -> bool:
        """Store the activity unless a newer one is stored; True if stored."""
        position = state.position
        async with self.store.transaction() as tx:
            return await tx.put_status(
                MowerStatus(
                    mower_id=state.mower_id,
                    account_id=state.account_id,
                    activity=state.activity.value,
                    observed_at=state.observed_at,
                    latitude=position.lat if position else None,
                    longitude=position.lon if position else None,
                )
            )

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
