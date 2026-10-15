"""Routes of the Planner under ``/api/v1/planner``."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Path, Response
from mowdeck_service_kit import CurrentAccountId, CurrentPrincipal

from planner.adapters.api.deps import ServicesDep, SpotMowing
from planner.adapters.api.schemas import (
    GapsOut,
    GapsRequestIn,
    PatternList,
    PatternOut,
    PlanOut,
    PlanRequestIn,
    PreviewOut,
    TemplateIn,
    TemplateList,
    TemplateOut,
)
from planner.application.planning import AreaSpec, GapsInput, PlanInput
from planner.application.services import Services

router = APIRouter(prefix="/planner", tags=["planner"])

NameParam = Annotated[str, Path(min_length=1, max_length=100)]


@router.get("/patterns")
async def list_patterns(_: CurrentPrincipal, services: ServicesDep) -> PatternList:
    """Every registered pattern with its settings schema (plug-ins included)."""
    items = []
    for pattern in services.registry.all():
        items.append(
            PatternOut(
                key=pattern.key,
                name=pattern.name,
                description=getattr(pattern, "description", None) or None,
                settings_schema=pattern.settings_schema(),
                native=getattr(pattern, "native_name", None),
            )
        )
    return PatternList(items=items)


async def _plan_input(body: PlanRequestIn, account_id: UUID, services: Services) -> PlanInput:
    if body.template is not None:
        template = await services.templates.get(account_id, body.template)
        chain = [(str(s["pattern"]), dict(s.get("settings") or {})) for s in template.chain]
    else:
        chain = [(step.pattern, dict(step.settings)) for step in body.chain]
    return PlanInput(
        chain=chain,
        area=body.area.spec(),
        mower_id=body.mower_id,
        raw_map=body.map,
        mower_overrides=body.mower.overrides() if body.mower else {},
        wgs84_preview=body.wgs84,
        transit=body.transit.start() if body.transit is not None else None,
        turn_style=body.turn_style,
    )


@router.post("/plans/preview")
async def preview_plan(
    body: PlanRequestIn,
    principal: SpotMowing,
    account_id: CurrentAccountId,
    services: ServicesDep,
) -> PreviewOut:
    """Compute and validate a plan and show it: segments, duration, area, warnings, and how
    the mower can run it. Needs ``can_view`` on the mower (404 when invisible)."""
    request = await _plan_input(body, account_id, services)
    result = await services.planning.compute(principal, account_id, request, for_start=False)
    return PreviewOut.of(result)


@router.post("/plans")
async def create_plan(
    body: PlanRequestIn,
    principal: SpotMowing,
    account_id: CurrentAccountId,
    services: ServicesDep,
) -> PlanOut:
    """The plan ready to start: the ``SpotMowPlan`` with its hash and the ``start_mow``
    command for Fleet. Nothing is stored; the same request gives the same hash. Needs
    ``can_control`` on the mower (Fleet checks it again when the command arrives)."""
    request = await _plan_input(body, account_id, services)
    result = await services.planning.compute(principal, account_id, request, for_start=True)
    return PlanOut.of(result)


@router.post("/coverage/gaps")
async def coverage_gaps(
    body: GapsRequestIn,
    principal: SpotMowing,
    account_id: CurrentAccountId,
    services: ServicesDep,
) -> GapsOut:
    """Covered ground and the gaps of an area from the mower's position track (Telemetry)
    between ``start`` and ``end``, with a ``remow`` step for them."""
    area = body.area.spec() if body.area is not None else AreaSpec()
    result = await services.planning.gaps(
        principal,
        account_id,
        GapsInput(
            mower_id=body.mower_id,
            start=body.start,
            end=body.end,
            area=area,
            min_gap_m2=body.min_gap_m2,
            cutting_width_m=body.cutting_width_m,
        ),
    )
    return GapsOut.of(result)


@router.get("/templates")
async def list_templates(
    _: CurrentPrincipal, account_id: CurrentAccountId, services: ServicesDep
) -> TemplateList:
    """Saved chains of the caller's account."""
    found = await services.templates.all(account_id)
    return TemplateList(items=[TemplateOut.of(t) for t in found])


@router.get("/templates/{name}")
async def get_template(
    name: NameParam, _: CurrentPrincipal, account_id: CurrentAccountId, services: ServicesDep
) -> TemplateOut:
    """One saved chain."""
    return TemplateOut.of(await services.templates.get(account_id, name))


@router.put("/templates/{name}")
async def put_template(
    name: NameParam,
    body: TemplateIn,
    principal: SpotMowing,
    account_id: CurrentAccountId,
    services: ServicesDep,
    response: Response,
) -> TemplateOut:
    """Create (201) or replace (200) a saved chain; needs ``can_control`` on the account."""
    template, created = await services.templates.put(
        principal,
        account_id,
        name,
        [step.model_dump() for step in body.chain],
        body.description,
    )
    response.status_code = 201 if created else 200
    return TemplateOut.of(template)


@router.delete("/templates/{name}", status_code=204)
async def delete_template(
    name: NameParam, principal: SpotMowing, account_id: CurrentAccountId, services: ServicesDep
) -> Response:
    """Delete a saved chain; needs ``can_control`` on the account."""
    await services.templates.delete(principal, account_id, name)
    return Response(status_code=204)
