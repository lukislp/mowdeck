"""Public demo (ADR 0068): past mowing days for the freshly seeded demo mowers.

Only the demo seed job calls this (404 outside demo mode, 403 without the seed secret), before
the Device-Gateway starts: the simulated days run offline (``mowdeck_control.history``) and go
through the normal ingestion, so tracks, coverage, heatmaps and the health history look like a
mower that worked for a week. Hidden from the API documents.
"""

from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends
from mowdeck_contracts.v1 import utc_now
from mowdeck_control.history import MAX_DAYS, simulated_history
from mowdeck_service_kit.demo import require_demo_seed
from pydantic import BaseModel, Field

from telemetry.adapters.api.deps import ServicesDep

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
    """Simulate ``days`` past mowing days per mower and ingest them, oldest first."""
    offered = stored = 0
    now = utc_now()
    for mower in body.mowers:
        states = await simulated_history(
            mower_id=mower.mower_id,
            account_id=mower.account_id,
            options=mower.connection_options,
            end=now,
            days=body.days,
        )
        for state in states:
            offered += 1
            if await services.ingest.record_state(state):
                stored += 1
    return DemoHistoryOut(states=offered, stored=stored)
