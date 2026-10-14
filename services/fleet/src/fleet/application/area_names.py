"""Custom names of map areas per mower: read them, set or drop one.

The mower keeps its own names (MowDeck never writes to the mower); every MowDeck surface shows
the custom name first, then the mower's own name, then the numbered fallback "Area N"
(``mowdeck_contracts.v1.area_display_name``). After a change Fleet publishes
``fleet.mower.area_names_changed`` with all names of the mower, so other services can refresh.
"""

from uuid import UUID

from mowdeck_contracts.v1 import FLEET_AREA_NAMES_CHANGED, AreaNamesChanged, clean_area_name

from fleet.application.context import Actor
from fleet.application.ports import Clock, Store, Tx
from fleet.application.work import UnitOfWork
from fleet.domain.errors import FieldProblem, InvalidInput, NotFoundError
from fleet.domain.models import MowerAreaName


async def names_of(tx: Tx, mower_id: UUID) -> dict[str, str]:
    """The custom names of one mower, by area id (sorted by id)."""
    rows = await tx.area_names.find(mower_id=mower_id, order_by="area_id")
    return {row.area_id: row.name for row in rows}


async def names_by_mower(tx: Tx, account_id: UUID) -> dict[UUID, dict[str, str]]:
    """The custom names of every mower of the account."""
    found: dict[UUID, dict[str, str]] = {}
    for row in await tx.area_names.find(account_id=account_id, order_by="area_id"):
        found.setdefault(row.mower_id, {})[row.area_id] = row.name
    return found


class AreaNameService:
    """Use cases of custom area names. The routes check the relations (``can_view`` to read,
    ``can_control`` to change); rows are always loaded with the account filter."""

    def __init__(self, uow: UnitOfWork, store: Store, clock: Clock) -> None:
        self.uow = uow
        self.store = store
        self.clock = clock

    async def get(self, account_id: UUID, mower_id: UUID) -> dict[str, str]:
        """The custom names of a mower of the account (404 for any other account)."""
        async with self.store.transaction() as tx:
            if await tx.mowers.get(id=mower_id, account_id=account_id) is None:
                raise NotFoundError("mower not found")
            return await names_of(tx, mower_id)

    async def set(
        self, actor: Actor, account_id: UUID, mower_id: UUID, area_id: str, name: str | None
    ) -> dict[str, str]:
        """Set the custom name of ``area_id``; None or blank drops it (the mower's own name or
        the fallback shows again). Returns all names of the mower after the change; an
        unchanged name changes nothing (no audit row, no event).

        Any area id is accepted: the map may be newer than the snapshot the client saw, and a
        name for an area the mower no longer has is simply never shown.
        """
        try:
            cleaned = clean_area_name(name)
        except ValueError as exc:
            problem = FieldProblem("name", str(exc), "too_long")
            raise InvalidInput(str(exc), fields=[problem]) from None
        async with self.uow() as work:
            if await work.tx.mowers.get(id=mower_id, account_id=account_id) is None:
                raise NotFoundError("mower not found")
            key = {"mower_id": mower_id, "area_id": area_id}
            current = await work.tx.area_names.get(**key)
            previous = current.name if current is not None else None
            if previous == cleaned:
                return await names_of(work.tx, mower_id)
            now = self.clock()
            if cleaned is None:
                await work.tx.area_names.delete(**key)
            elif current is None:
                await work.tx.area_names.add(
                    MowerAreaName(
                        mower_id=mower_id,
                        area_id=area_id,
                        account_id=account_id,
                        name=cleaned,
                        updated_at=now,
                        updated_by=actor.subject_id,
                    )
                )
            else:
                values = {"name": cleaned, "updated_at": now, "updated_by": actor.subject_id}
                await work.tx.area_names.update_where(values, **key)
            names = await names_of(work.tx, mower_id)
            await work.audit(
                actor,
                "mower.area_renamed",
                account_id,
                "mower",
                mower_id,
                {"area_id": area_id, "name": cleaned, "previous": previous},
            )
            work.emit(
                FLEET_AREA_NAMES_CHANGED,
                account_id,
                AreaNamesChanged(mower_id=mower_id, account_id=account_id, names=names),
            )
        return names
