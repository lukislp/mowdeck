"""Routes for mowers and their commands under ``/api/v1/fleet``."""

from datetime import datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Path, Query, Request, Response
from mowdeck_authz import mower
from mowdeck_authz.fastapi import authorize
from mowdeck_contracts.v1 import (
    CameraSession,
    CommandAccepted,
    CommandRequest,
    CommandView,
    DriveSessionView,
    MowPath,
    PathPreviewRequest,
)
from mowdeck_service_kit import CurrentAccountId
from pydantic import BaseModel

from fleet.adapters.api.deps import (
    ActorDep,
    ControlMower,
    ManageMower,
    ServicesDep,
    ViewMower,
    actor_of,
)
from fleet.adapters.api.schemas import (
    AreaNameIn,
    AreaNamesOut,
    ConnectionKindList,
    ConnectionKindOut,
    MowDefaultsIn,
    MowerGroupsIn,
    MowerIn,
    MowerList,
    MowerOut,
    MowerPatch,
)
from fleet.application.mowers import MowerInput
from fleet.domain.connections import SCHEMAS
from fleet.domain.rules import relation_for

router = APIRouter(prefix="/fleet", tags=["mowers"])


@router.get("/connection-kinds")
async def list_connection_kinds(_: ActorDep, services: ServicesDep) -> ConnectionKindList:
    """The connection kinds this installation accepts (``MOWDECK_ALLOWED_CONNECTION_KINDS``) with
    the option schema of each: required, optional, secret and alternative options. The web app
    generates its connection forms from it, so a new adapter kind needs no UI change."""
    allowed = services.mowers.config.allowed_connection_kinds
    known = [kind for kind in SCHEMAS if kind in allowed]
    others = sorted(allowed - set(SCHEMAS))
    return ConnectionKindList(items=[ConnectionKindOut.of(kind) for kind in known + others])


@router.get("/mowers")
async def list_mowers(
    actor: ActorDep,
    account_id: CurrentAccountId,
    services: ServicesDep,
    site_id: Annotated[UUID | None, Query()] = None,
    group_id: Annotated[UUID | None, Query()] = None,
) -> MowerList:
    """Mowers of the caller's account that the caller may view (``can_view`` per mower)."""
    found = await services.mowers.visible(actor, account_id, site_id=site_id, group_id=group_id)
    return MowerList(items=[MowerOut.of(m) for m in found])


@router.post("/mowers", status_code=201)
async def register_mower(
    body: MowerIn,
    actor: ActorDep,
    account_id: CurrentAccountId,
    services: ServicesDep,
    response: Response,
) -> MowerOut:
    """Register a mower: ``can_manage_mowers`` on the target site (the account for the default
    site) and on each group. Publishes ``mower.registered`` after the commit. A ``cloud`` mower
    names a linked account and its device; 409 ``mower-already-registered`` when that physical
    mower is registered already (in this or another account, which is not named)."""
    created = await services.mowers.register(
        actor,
        account_id,
        MowerInput(
            name=body.name or body.device_name or "",
            connection_kind=body.connection_kind.value,
            connection_options=body.connection_options,
            model=body.model,
            serial=body.serial,
            site_id=body.site_id,
            group_ids=tuple(body.group_ids),
            linked_account_id=body.linked_account_id,
            device_name=body.device_name,
        ),
    )
    response.headers["Location"] = f"/api/v1/fleet/mowers/{created.mower.id}"
    return MowerOut.of(created)


@router.get("/mowers/{mower_id}")
async def get_mower(
    mower_id: UUID, _: ViewMower, account_id: CurrentAccountId, services: ServicesDep
) -> MowerOut:
    """One mower (``can_view``, hidden otherwise)."""
    return MowerOut.of(await services.mowers.get(account_id, mower_id))


@router.patch("/mowers/{mower_id}")
async def update_mower(
    mower_id: UUID,
    body: MowerPatch,
    principal: ManageMower,
    account_id: CurrentAccountId,
    services: ServicesDep,
) -> MowerOut:
    """Change name, model, serial, connection or site (``can_manage_mowers``; a new site needs
    it too). Publishes ``mower.updated`` when name, connection or site changed."""
    changes = body.model_dump(include=body.model_fields_set)
    if body.connection_kind is not None:
        changes["connection_kind"] = body.connection_kind.value
    found = await services.mowers.update(actor_of(principal), account_id, mower_id, changes)
    return MowerOut.of(found)


@router.put("/mowers/{mower_id}/groups")
async def set_mower_groups(
    mower_id: UUID,
    body: MowerGroupsIn,
    principal: ManageMower,
    account_id: CurrentAccountId,
    services: ServicesDep,
) -> MowerOut:
    """Make the mower a member of exactly these groups (``can_manage_mowers`` on the mower and
    on every added or removed group)."""
    found = await services.mowers.set_groups(
        actor_of(principal), account_id, mower_id, body.group_ids
    )
    return MowerOut.of(found)


AreaId = Annotated[str, Path(min_length=1, max_length=128, description="`MowerMap.areas[].id`")]


@router.get("/mowers/{mower_id}/area-names")
async def get_area_names(
    mower_id: UUID, _: ViewMower, account_id: CurrentAccountId, services: ServicesDep
) -> AreaNamesOut:
    """The custom names of the mower's map areas (``can_view``, hidden otherwise). The same
    names come with the mower (``MowerOut.area_names``)."""
    return AreaNamesOut(names=await services.area_names.get(account_id, mower_id))


@router.put(
    "/mowers/{mower_id}/area-names/{area_id}",
    responses={
        403: {"description": "The caller sees the mower but has no `can_control`."},
        404: {"description": "The mower does not exist or is not visible to the caller."},
        422: {"description": "The name has more than 60 characters after trimming."},
    },
)
async def set_area_name(
    mower_id: UUID,
    area_id: AreaId,
    body: AreaNameIn,
    principal: ControlMower,
    account_id: CurrentAccountId,
    services: ServicesDep,
) -> AreaNamesOut:
    """Name one map area in MowDeck (``can_control``; 403 when the mower is visible, else 404).
    ``null`` or a blank name drops the custom name. The mower keeps its own names: nothing is
    sent to the mower. Publishes ``fleet.mower.area_names_changed`` after a change; answers all
    custom names of the mower."""
    names = await services.area_names.set(
        actor_of(principal), account_id, mower_id, area_id, body.name
    )
    return AreaNamesOut(names=names)


@router.put(
    "/mowers/{mower_id}/mow-defaults",
    responses={
        403: {"description": "The caller sees the mower but has no `can_control`."},
        404: {"description": "The mower does not exist or is not visible to the caller."},
        422: {"description": "Unknown pattern, a value out of range or an unknown key."},
    },
)
async def set_mow_defaults(
    mower_id: UUID,
    body: MowDefaultsIn,
    principal: ControlMower,
    account_id: CurrentAccountId,
    services: ServicesDep,
) -> MowerOut:
    """Keep the mowing defaults of the mower (``can_control``; 403 when the mower is visible,
    else 404): the pattern settings every client starts with (web start sheet, Home Assistant,
    schedules). ``pattern: null`` drops them. Nothing is sent to the mower; a start still
    names its own pattern in ``StartMow.pattern``. Whether the mower runs the pattern is its
    capabilities' business (``native_patterns``). Answers the mower with ``mow_defaults``."""
    found = await services.mowers.set_mow_defaults(
        actor_of(principal), account_id, mower_id, body.to_domain()
    )
    return MowerOut.of(found)


@router.delete("/mowers/{mower_id}", status_code=204)
async def delete_mower(
    mower_id: UUID, principal: ManageMower, account_id: CurrentAccountId, services: ServicesDep
) -> None:
    """Remove the mower and its tuples; publishes ``mower.removed``."""
    await services.mowers.delete(actor_of(principal), account_id, mower_id)


@router.post("/mowers/{mower_id}/commands", status_code=202)
async def send_command(
    mower_id: UUID,
    body: CommandRequest,
    request: Request,
    response: Response,
    account_id: CurrentAccountId,
    services: ServicesDep,
) -> CommandAccepted:
    """Send a command. The relation depends on the kind: ``can_control`` (start, pause,
    resume, dock, blade height, speed), ``can_drive`` (manual drive), ``can_emergency_stop``
    (emergency stop, equals ``can_view``). Checked before any database or bus access: 403 when
    the mower is visible, 404 when not."""
    principal = await authorize(request, relation_for(body.command.kind), mower(mower_id))
    accepted = await services.commands.send(actor_of(principal), account_id, mower_id, body)
    response.headers["Location"] = accepted.status_url
    return accepted


class CommandPageOut(BaseModel):
    """``GET /mowers/{id}/commands``: newest first; pass ``next_before`` as ``before``."""

    items: list[CommandView]
    next_before: datetime | None


@router.get("/mowers/{mower_id}/commands")
async def list_mower_commands(
    mower_id: UUID,
    _: ViewMower,
    account_id: CurrentAccountId,
    services: ServicesDep,
    before: Annotated[datetime | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> CommandPageOut:
    """Command records of the mower (``can_view``, hidden otherwise)."""
    page = await services.commands.list_for_mower(account_id, mower_id, before=before, limit=limit)
    return CommandPageOut(items=page.items, next_before=page.next_before)


class DriveSessionPageOut(BaseModel):
    """``GET /mowers/{id}/drive-sessions``: newest first; pass ``next_before`` as ``before``."""

    items: list[DriveSessionView]
    next_before: datetime | None


@router.get("/mowers/{mower_id}/drive-sessions")
async def list_drive_sessions(
    mower_id: UUID,
    _: ViewMower,
    account_id: CurrentAccountId,
    services: ServicesDep,
    before: Annotated[datetime | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> DriveSessionPageOut:
    """Manual drive sessions of the mower (the joystick audit: who, when, how it ended, frame
    counts; ``can_view``, hidden otherwise). Frames themselves are not stored."""
    page = await services.drive.list_for_mower(account_id, mower_id, before=before, limit=limit)
    return DriveSessionPageOut(items=page.items, next_before=page.next_before)


@router.post(
    "/mowers/{mower_id}/camera/session",
    responses={
        403: {"description": "The plan does not include the feature `camera`."},
        404: {"description": "The mower does not exist or is not visible to the caller."},
        409: {"description": "`camera-unsupported`: the mower's connection has no camera."},
        503: {"description": "The Device-Gateway or the vendor cloud did not answer."},
    },
)
async def open_camera_session(
    mower_id: UUID,
    principal: ViewMower,
    account_id: CurrentAccountId,
    services: ServicesDep,
    response: Response,
) -> CameraSession:
    """A short-lived live camera session (ADR 0032): ``can_view`` on the mower (hidden
    otherwise) and the plan feature ``camera``. Fleet relays the request to the Device-Gateway
    that holds the mower and audits it (``mower.camera_opened``). The browser joins the video
    itself (``provider`` ``agora``: Agora Web SDK; ``fake``: a test pattern); no media passes
    through MowDeck. The token is a secret: the response is never cached; ask again before
    ``expires_at``."""
    response.headers["Cache-Control"] = "no-store"
    return await services.camera.open(actor_of(principal), account_id, mower_id)


@router.post(
    "/mowers/{mower_id}/path-preview",
    responses={
        403: {"description": "The plan does not include the feature `control`."},
        404: {"description": "The mower does not exist or is not visible to the caller."},
        409: {
            "description": "`path-preview-unsupported`: the connection cannot plan without "
            "starting; `path-preview-rejected`: a job runs, or the areas or pattern do not fit."
        },
        503: {"description": "The Device-Gateway did not answer or the mower is offline."},
        504: {"description": "`gateway-timeout`: the mower did not hand back its route in time."},
    },
)
async def preview_mow_path(
    mower_id: UUID,
    body: PathPreviewRequest,
    principal: ControlMower,
    account_id: CurrentAccountId,
    services: ServicesDep,
    response: Response,
) -> MowPath:
    """Let the mower plan the route of a start (areas and pattern as for ``start_mow``) without
    starting it, as the vendor app does before a start: ``can_control`` on the mower (hidden
    otherwise) and the plan feature ``control``. Fleet relays the request to the Device-Gateway
    that holds the mower; planning and fetching the route take a few seconds to about a minute
    (over 4G longer), so clients show progress and allow up to 90 s. The route is not stored;
    the start uses the same areas and pattern and the mower plans the same route again."""
    response.headers["Cache-Control"] = "no-store"
    return await services.path_preview.preview(actor_of(principal), account_id, mower_id, body)


@router.get("/commands/{command_id}")
async def get_command(
    command_id: UUID, actor: ActorDep, account_id: CurrentAccountId, services: ServicesDep
) -> CommandView:
    """One command record of the caller's account whose mower the caller may view (404
    otherwise)."""
    return await services.commands.get(actor, account_id, command_id)
