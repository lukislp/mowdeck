"""The angle rotation of one mower (ADR 0061): its last angle per area and the step the start
sheet offers for manual starts.

Scheduled runs store their angle themselves (``runner``). A manual start from the start sheet
does not pass the Scheduler; when the user chose rotation there, the client reports the angle it
sent (``record_manual``), so the next run, scheduled or manual, continues from it. A manual
start with an explicit angle is not reported and leaves the rotation where it was.

The routes check the rights (``can_view`` to read, ``can_control`` to change); every row is
read and written with the caller's account.
"""

from collections.abc import Sequence
from dataclasses import dataclass, field
from uuid import UUID

from scheduler.application.ports import Clock, Store
from scheduler.domain.errors import InvalidInput
from scheduler.domain.rotation import AreaAngle, MowerRotation, normalised, valid_step


@dataclass(frozen=True, slots=True)
class MowerAngles:
    """Where the rotation of one mower stands."""

    mower_id: UUID
    manual_step_deg: float | None
    areas: list[AreaAngle] = field(default_factory=list)


class AngleService:
    """Use cases of a mower's angle rotation."""

    def __init__(self, store: Store, clock: Clock) -> None:
        self.store = store
        self.clock = clock

    async def get(self, account_id: UUID, mower_id: UUID) -> MowerAngles:
        """The last angles and the manual step of a mower (nothing stored: empty)."""
        async with self.store.transaction() as tx:
            areas = await tx.get_area_angles(mower_id)
            rotation = await tx.get_rotation(mower_id)
        return MowerAngles(
            mower_id=mower_id,
            manual_step_deg=(
                rotation.manual_step_deg
                if rotation is not None and rotation.account_id == account_id
                else None
            ),
            areas=[a for a in areas if a.account_id == account_id],
        )

    async def set_manual_step(
        self, account_id: UUID, mower_id: UUID, step: float | None
    ) -> MowerAngles:
        """Set (or with None switch off) the step of manual starts."""
        if step is not None and not valid_step(step):
            raise InvalidInput("manual_step_deg must be between 1 and 179 degrees")
        async with self.store.transaction() as tx:
            await tx.put_rotation(
                MowerRotation(
                    mower_id=mower_id,
                    account_id=account_id,
                    manual_step_deg=step,
                    updated_at=self.clock(),
                )
            )
        return await self.get(account_id, mower_id)

    async def record_manual(
        self, account_id: UUID, mower_id: UUID, area_ids: Sequence[str], angle: float
    ) -> MowerAngles:
        """A manual start with rotation chosen mowed ``area_ids`` with ``angle``."""
        if any(not 1 <= len(a) <= 128 for a in area_ids):
            raise InvalidInput("area_ids: ids of 1 to 128 characters")
        now = self.clock()
        async with self.store.transaction() as tx:
            await tx.put_area_angles(
                [
                    AreaAngle(
                        mower_id=mower_id,
                        area_id=area_id,
                        account_id=account_id,
                        angle_deg=normalised(angle),
                        used_at=now,
                        source="manual",
                    )
                    for area_id in dict.fromkeys(area_ids)
                ]
            )
        return await self.get(account_id, mower_id)
