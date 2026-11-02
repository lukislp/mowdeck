"""What Entitlements refuses in the public demo (ADR 0068): every change of plans and account
plans (operator actions; the demo account keeps its plan). And the sandbox purge (ADR 0076).

``DELETE /entitlements/demo/accounts/{account_id}`` is for Identity's sandbox cleanup only (404
outside demo mode, 403 without the seed secret) and hidden from the API documents. The policy
needs no exception for it: a request with the seed secret passes the ``allow_only`` policy, and
without the secret the policy refuses it before the route runs.
"""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends
from mowdeck_service_kit.demo import DemoPolicy, DemoPurgeOut, require_demo_seed

from entitlements.adapters.api.deps import ServicesDep

DEMO_POLICY = DemoPolicy(
    allow_only=(),
    default_reason="Plans cannot be changed in the demo.",
)

router = APIRouter(prefix="/entitlements/demo", tags=["entitlements: demo"])


@router.delete("/accounts/{account_id}", include_in_schema=False)
async def demo_purge(
    account_id: UUID,
    _: Annotated[None, Depends(require_demo_seed)],
    services: ServicesDep,
) -> DemoPurgeOut:
    """Delete the plan row and the assignment history of an expired demo sandbox account.
    Idempotent."""
    return DemoPurgeOut(deleted=await services.entitlements.purge_account(account_id))
