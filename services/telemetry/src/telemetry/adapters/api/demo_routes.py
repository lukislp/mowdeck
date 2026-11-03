"""Public demo (ADR 0068): past mowing days for the freshly seeded demo mowers.

Only the demo seed job and Identity's sandbox pool call this (404 outside demo mode, 403
without the seed secret), before the mowers report: the simulated days run offline
(``mowdeck_control.history``) once a day per garden through the normal ingestion and are copied
to each mower (:mod:`telemetry.application.demo_history`, ADR 0079), so tracks, coverage,
heatmaps and the health history look like a mower that worked for a week. Hidden from the API
documents.
"""

from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends
from mowdeck_control.history import MAX_DAYS
from mowdeck_service_kit.demo import DemoPurgeOut, require_demo_seed
from mowdeck_service_kit.errors import ServiceUnavailable
from pydantic import BaseModel, Field

from telemetry.adapters.api.deps import ServicesDep
from telemetry.application.demo_history import HistoryMower

router = APIRouter(prefix="/telemetry/demo", tags=["telemetry: demo"])


class DemoMowerIn(BaseModel):
    """One simulated mower of the demo account."""

    mower_id: UUID
    account_id: UUID
    connection_options: dict[str, Any] = Field(default_factory=dict)


class DemoHistoryIn(BaseModel):
    """The mowers and how many days back."""

    mowers: list[DemoMowerIn] = Field(min_length=1, max_length=10)
    days: int = Field(default=7, ge=1, le=MAX_DAYS)


class DemoHistoryOut(BaseModel):
    """How many states were offered to the ingestion, and how many it stored."""

    states: int
    stored: int


@router.post("/history", include_in_schema=False)
async def demo_history(
    body: DemoHistoryIn,
    _: Annotated[None, Depends(require_demo_seed)],
    services: ServicesDep,
) -> DemoHistoryOut:
    """``days`` past mowing days per mower, oldest first: simulated once a day per garden and
    copied to every mower of that garden (ADR 0079)."""
    if services.demo_history is None:
        raise ServiceUnavailable("telemetry is starting")
    result = await services.demo_history.fill(
        [HistoryMower(m.mower_id, m.account_id, m.connection_options) for m in body.mowers],
        body.days,
    )
    return DemoHistoryOut(states=result.states, stored=result.stored)


@router.delete("/accounts/{account_id}", include_in_schema=False)
async def demo_purge(
    account_id: UUID,
    _: Annotated[None, Depends(require_demo_seed)],
    services: ServicesDep,
) -> DemoPurgeOut:
    """Delete everything telemetry holds of an expired demo sandbox account (ADR 0076):
    latest states, history, positions, commands, snapshots, health, zones, tracking sessions
    and the mower directory. Idempotent."""
    if services.demo is None:
        raise ServiceUnavailable("telemetry is starting")
    return DemoPurgeOut(deleted=await services.demo.purge_account(account_id))
