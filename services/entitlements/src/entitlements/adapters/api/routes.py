"""Routes of entitlements under ``/api/v1/entitlements``.

==========================  =====================  ==========================================
Route                       Who                    What
==========================  =====================  ==========================================
GET  /me                    any principal of an    the plan of the token's account
                            account
GET  /features              any principal          the feature catalogue
GET  /plans                 public                 the offered (active) plans
GET  /plans/{key}           operator               one plan, also inactive, unknown features
PUT  /plans/{key}           operator               create or replace a plan
GET  /accounts              operator               accounts with plan and status (paged)
GET  /accounts/{id}         operator               one account with its history
PUT  /accounts/{id}/plan    operator               assign a plan by hand (with a reason)
==========================  =====================  ==========================================

Operator: ``can_manage_plans`` on ``platform:main``. No route returns mower data.
"""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Query
from mowdeck_service_kit import CurrentAccountId, CurrentPrincipal

from entitlements.adapters.api.deps import Operator, ServicesDep, actor_of
from entitlements.adapters.api.schemas import (
    AccountDetailOut,
    AccountOut,
    AccountPageOut,
    AssignmentIn,
    FeatureOut,
    PlanDetailOut,
    PlanIn,
    PlanOut,
    ResolvedOut,
)
from entitlements.application.entitlements import Assignment

router = APIRouter(prefix="/entitlements", tags=["entitlements"])


@router.get("/me")
async def me(account_id: CurrentAccountId, services: ServicesDep) -> ResolvedOut:
    """The plan, status, limits and features that apply to the caller's account now."""
    return ResolvedOut.of(await services.entitlements.resolve(account_id))


@router.get("/features")
async def features(_: CurrentPrincipal, services: ServicesDep) -> list[FeatureOut]:
    """Every feature key the modules declare (retired keys included)."""
    return [FeatureOut.of(e) for e in await services.catalogue.entries()]


@router.get("/plans")
async def plans(services: ServicesDep) -> list[PlanOut]:
    """The plans on offer (public: a pricing page needs no login)."""
    return [PlanOut.of(p) for p in await services.entitlements.plans()]


@router.get("/plans/{key}")
async def get_plan(key: str, _: Operator, services: ServicesDep) -> PlanDetailOut:
    """One plan (operators), also an inactive one."""
    plan = await services.entitlements.plan(key)
    return PlanDetailOut.detail(plan, await services.entitlements.unknown_features_of(plan))


@router.put("/plans/{key}")
async def put_plan(key: str, body: PlanIn, _: Operator, services: ServicesDep) -> PlanDetailOut:
    """Create or replace a plan (operators); every service drops its cached snapshots."""
    plan = await services.entitlements.put_plan(body.spec(key))
    return PlanDetailOut.detail(plan, await services.entitlements.unknown_features_of(plan))


@router.get("/accounts")
async def accounts(
    _: Operator,
    services: ServicesDep,
    offset: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> AccountPageOut:
    """Accounts with a plan row, oldest first, with their effective plans (operators)."""
    page = await services.entitlements.accounts(offset, limit)
    return AccountPageOut(
        items=[AccountOut.of(item) for item in page.items],
        total=page.total,
        offset=page.offset,
        limit=page.limit,
        billing_enabled=services.entitlements.config.billing_enabled,
    )


@router.get("/accounts/{account_id}")
async def get_account(account_id: UUID, _: Operator, services: ServicesDep) -> AccountDetailOut:
    """One account: what applies, the assignment and its history (operators)."""
    return AccountDetailOut.of(await services.entitlements.account(account_id))


@router.put("/accounts/{account_id}/plan")
async def assign_plan(
    account_id: UUID, body: AssignmentIn, principal: Operator, services: ServicesDep
) -> AccountDetailOut:
    """Assign a plan by hand (operators): history row, ``entitlements.plan.changed``."""
    detail = await services.entitlements.assign(
        account_id,
        Assignment(
            plan=body.plan,
            reason=body.reason,
            status=body.status,
            trial_until=body.trial_until,
            grace_until=body.grace_until,
            account_name=body.account_name,
        ),
        actor_of(principal),
    )
    return AccountDetailOut.of(detail)
