"""Routes for sites and groups under ``/api/v1/fleet`` (with their bulk commands)."""

from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Query, Request, Response
from mowdeck_authz import group, site
from mowdeck_authz.fastapi import authorize
from mowdeck_contracts.v1 import BulkCommandAccepted, CommandRequest
from mowdeck_service_kit import CurrentAccountId

from fleet.adapters.api.deps import (
    ActorDep,
    ManageAccount,
    ManageGroup,
    ManageSite,
    ServicesDep,
    ViewGroup,
    ViewSite,
    actor_of,
)
from fleet.adapters.api.schemas import (
    GroupIn,
    GroupList,
    GroupOut,
    GroupPatch,
    SiteIn,
    SiteList,
    SiteOut,
    SitePatch,
)
from fleet.application.services import Services
from fleet.domain.rules import bulk_gate_relation

router = APIRouter(prefix="/fleet", tags=["sites and groups"])


# --- sites ------------------------------------------------------------------------------------


@router.get("/sites")
async def list_sites(
    actor: ActorDep,
    account_id: CurrentAccountId,
    services: ServicesDep,
    include_default: Annotated[bool, Query()] = False,
) -> SiteList:
    """Sites of the caller's account that the caller may view (``can_view`` per site)."""
    found = await services.sites.visible(actor, account_id, include_default=include_default)
    return SiteList(items=[SiteOut.of(s) for s in found])


@router.post("/sites", status_code=201)
async def create_site(
    body: SiteIn,
    principal: ManageAccount,
    account_id: CurrentAccountId,
    services: ServicesDep,
    response: Response,
) -> SiteOut:
    """A new site (``can_manage_mowers`` on the account, else 403)."""
    created = await services.sites.create(
        actor_of(principal),
        account_id,
        name=body.name,
        timezone=body.timezone,
        address=body.address.model_dump() if body.address else None,
        latitude=body.latitude,
        longitude=body.longitude,
    )
    response.headers["Location"] = f"/api/v1/fleet/sites/{created.id}"
    return SiteOut.of(created)


@router.get("/sites/{site_id}")
async def get_site(
    site_id: UUID, _: ViewSite, account_id: CurrentAccountId, services: ServicesDep
) -> SiteOut:
    """One site (``can_view``, hidden otherwise)."""
    return SiteOut.of(await services.sites.get(account_id, site_id))


@router.patch("/sites/{site_id}")
async def update_site(
    site_id: UUID,
    body: SitePatch,
    principal: ManageSite,
    account_id: CurrentAccountId,
    services: ServicesDep,
) -> SiteOut:
    """Change name, timezone, address or coordinates (``can_manage_mowers``; the default site
    keeps its name)."""
    changes = body.model_dump(include=body.model_fields_set)
    return SiteOut.of(
        await services.sites.update(actor_of(principal), account_id, site_id, changes)
    )


@router.delete("/sites/{site_id}", status_code=204)
async def delete_site(
    site_id: UUID, principal: ManageSite, account_id: CurrentAccountId, services: ServicesDep
) -> None:
    """Delete an empty site (409 for the default site or while mowers or groups use it)."""
    await services.sites.delete(actor_of(principal), account_id, site_id)


# --- groups -----------------------------------------------------------------------------------


@router.get("/groups")
async def list_groups(
    actor: ActorDep,
    account_id: CurrentAccountId,
    services: ServicesDep,
    site_id: Annotated[UUID | None, Query()] = None,
) -> GroupList:
    """Groups of the caller's account that the caller may view, optionally of one site."""
    found = await services.groups.visible(actor, account_id, site_id=site_id)
    return GroupList(items=[GroupOut.of(g) for g in found])


@router.post("/groups", status_code=201)
async def create_group(
    body: GroupIn,
    actor: ActorDep,
    account_id: CurrentAccountId,
    services: ServicesDep,
    response: Response,
) -> GroupOut:
    """A new group (``can_manage_mowers`` on the parent site, or the account if cross-site)."""
    created = await services.groups.create(actor, account_id, name=body.name, site_id=body.site_id)
    response.headers["Location"] = f"/api/v1/fleet/groups/{created.id}"
    return GroupOut.of(created, [])


@router.get("/groups/{group_id}")
async def get_group(
    group_id: UUID, principal: ViewGroup, account_id: CurrentAccountId, services: ServicesDep
) -> GroupOut:
    """One group with the mowers the caller may view (``can_view``, hidden otherwise)."""
    item, mower_ids = await services.groups.get(actor_of(principal), account_id, group_id)
    return GroupOut.of(item, mower_ids)


@router.patch("/groups/{group_id}")
async def update_group(
    group_id: UUID,
    body: GroupPatch,
    principal: ManageGroup,
    account_id: CurrentAccountId,
    services: ServicesDep,
) -> GroupOut:
    """Rename or move to another site (``can_manage_mowers`` on the group and the new site)."""
    changes = body.model_dump(include=body.model_fields_set)
    item = await services.groups.update(actor_of(principal), account_id, group_id, changes)
    return GroupOut.of(item)


@router.delete("/groups/{group_id}", status_code=204)
async def delete_group(
    group_id: UUID, principal: ManageGroup, account_id: CurrentAccountId, services: ServicesDep
) -> None:
    """Delete an empty group (409 while it has mowers)."""
    await services.groups.delete(actor_of(principal), account_id, group_id)


# --- bulk commands ----------------------------------------------------------------------------


async def _bulk(
    request: Request,
    services: Services,
    account_id: UUID,
    target_type: Literal["site", "group"],
    target_id: UUID,
    body: CommandRequest,
) -> BulkCommandAccepted:
    relation = bulk_gate_relation(body.command.kind)
    target = site(target_id) if target_type == "site" else group(target_id)
    principal = await authorize(request, relation, target)  # 403 or 404 before any I/O
    return await services.commands.send_bulk(
        actor_of(principal), account_id, target_type, target_id, body
    )


@router.post("/sites/{site_id}/commands", status_code=202)
async def bulk_site_command(
    site_id: UUID,
    body: CommandRequest,
    request: Request,
    account_id: CurrentAccountId,
    services: ServicesDep,
) -> BulkCommandAccepted:
    """The command for every mower of the site, checked per mower.

    Gate: ``can_bulk_command`` on the site (``can_emergency_stop``, i.e. view, for an
    emergency stop). Per mower: the relation of the command kind; invisible mowers are omitted.
    """
    return await _bulk(request, services, account_id, "site", site_id, body)


@router.post("/groups/{group_id}/commands", status_code=202)
async def bulk_group_command(
    group_id: UUID,
    body: CommandRequest,
    request: Request,
    account_id: CurrentAccountId,
    services: ServicesDep,
) -> BulkCommandAccepted:
    """The command for every mower of the group, checked per mower (see the site variant)."""
    return await _bulk(request, services, account_id, "group", group_id, body)
