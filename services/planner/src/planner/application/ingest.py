"""Map and capability snapshots from the bus: the Planner's directory of mower facts."""

from mowdeck_contracts.v1 import MowerCapabilitiesSnapshot, MowerMapSnapshot

from planner.application.ports import Store


class Ingest:
    """Keeps the newest map and capabilities of every mower."""

    def __init__(self, store: Store) -> None:
        self.store = store

    async def on_map(self, snapshot: MowerMapSnapshot) -> bool:
        """Store the map unless a newer one is stored; True if stored."""
        async with self.store.transaction() as tx:
            return await tx.put_map(
                snapshot.mower_id,
                snapshot.account_id,
                snapshot.observed_at,
                snapshot.map.model_dump(mode="json"),
            )

    async def on_capabilities(self, snapshot: MowerCapabilitiesSnapshot) -> bool:
        """Store the capabilities unless newer ones are stored; True if stored."""
        async with self.store.transaction() as tx:
            return await tx.put_capabilities(
                snapshot.mower_id,
                snapshot.account_id,
                snapshot.observed_at,
                snapshot.capabilities.model_dump(mode="json"),
            )
