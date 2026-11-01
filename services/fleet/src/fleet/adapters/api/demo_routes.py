"""Demo sandboxes (ADR 0076): the purge of one expired sandbox account.

Only Identity's cleanup job calls this (404 outside demo mode, 403 without the seed secret).
Hidden from the API documents.
"""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends
from mowdeck_service_kit.demo import DEMO_PURGE_PATH, DemoPurgeOut, require_demo_seed

from fleet.adapters.api.deps import ServicesDep

router = APIRouter(prefix="/fleet", tags=["fleet: demo"])


@router.delete(DEMO_PURGE_PATH, include_in_schema=False)
async def purge_demo_account(
    account_id: UUID,
    _: Annotated[None, Depends(require_demo_seed)],
    services: ServicesDep,
) -> DemoPurgeOut:
    """Remove every mower (``mower.removed`` each), site, group, record and tuple of the
    account; a second call answers ``deleted`` 0."""
    return DemoPurgeOut(deleted=await services.purge.purge(account_id))
