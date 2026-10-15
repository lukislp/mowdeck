"""Routes for mowers and their commands under ``/api/v1/fleet``."""

from datetime import datetime
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Path, Query, Request, Response
from mowdeck_authz import mower
from mowdeck_authz.fastapi import authorize
from mowdeck_contracts.v1 import (
    BridgeScanReply,
    CameraSession,
    CommandAccepted,
    CommandRequest,
    CommandView,
    DriveCalibration,
    DriveSessionView,
    FirmwareInfo,
    MowPath,
    PathPreviewRequest,
)
from mowdeck_contracts.v1.links import LINK_BLE_BRIDGE
from mowdeck_service_kit import CurrentAccountId
from pydantic import BaseModel

from fleet.adapters.api.deps import (
    ActorDep,
    ControlMower,
    DriveMower,
    ManageMower,
    ServicesDep,
    ViewMower,
    actor_of,
)
from fleet.adapters.api.schemas import (
    AreaNameIn,
    AreaNamesOut,
    BridgeLinkIn,
    BridgeScanIn,
    ConnectionKindList,
    ConnectionKindOut,
    LinkOut,
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
    resume, dock, blade height, speed), ``can_drive`` (manual drive, spot mow, drive
    calibration, drive speed setting), ``can_emergency_stop``
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


# --- additional links (ADR 0038) -----------------------------------------------------------

_LINK_RESPONSES: dict[int | str, dict[str, Any]] = {
    403: {"description": "The caller sees the mower but has no `can_manage_mowers`."},
    404: {"description": "The mower does not exist or is not visible to the caller."},
    409: {
        "description": "`link-not-applicable`: the mower's connection kind carries no Bluetooth "
        "bridge link (decided by the connection-kind registry, `GET /connection-kinds`)."
    },
}
_SCAN_RESPONSES: dict[int | str, dict[str, Any]] = {
    **_LINK_RESPONSES,
    409: {
        "description": "`link-not-applicable`: the connection kind carries no bridge link; "
        "`bridge-scan-unsupported`: the Device-Gateway cannot scan through this bridge."
    },
    503: {"description": "No Device-Gateway answered in time or holds the mower right now."},
}


@router.post(
    "/mowers/{mower_id}/links/ble_bridge/scan",
    responses={
        **_SCAN_RESPONSES,
        422: {
            "description": "`***` as key without a stored link (`#/links/ble_bridge/noise_psk`)."
        },
    },
)
async def scan_bridge(
    mower_id: UUID,
    body: BridgeScanIn,
    _: ManageMower,
    account_id: CurrentAccountId,
    services: ServicesDep,
    response: Response,
) -> BridgeScanReply:
    """Look for the mower through a Bluetooth bridge before storing the link (``can_manage_mowers``,
    hidden otherwise). Fleet relays the request to the Device-Gateway that holds the mower; the
    gateway connects to the bridge at ``host``:``port`` with the key, reads the bridge's
    information and collects mower advertisements for up to 30 s (early when the expected name
    shows up). The answer is the gateway's ``BridgeScanReply``: ``proxy`` as soon as the bridge
    answered, ``devices`` seen so far, and ``error`` for the outcomes the user must act on
    (``unreachable``, ``auth_failed``, ``timeout``, ``busy``) — these are a 200 with the reply,
    so a timeout still shows the bridge and the other mowers seen. When the mower already has a
    link to the same bridge its ``proxy_info`` is refreshed. ``***`` as key means the stored key.
    The key is never returned or logged."""
    response.headers["Cache-Control"] = "no-store"
    return await services.links.scan(account_id, mower_id, LINK_BLE_BRIDGE, body.to_input())


@router.put(
    "/mowers/{mower_id}/links/ble_bridge",
    responses={
        **_LINK_RESPONSES,
        422: {
            "description": "An option the `ble_bridge` schema refuses (`#/links/ble_bridge/<key>`)."
        },
        503: {"description": "Fleet has no credential encryption key."},
    },
)
async def set_bridge_link(
    mower_id: UUID,
    body: BridgeLinkIn,
    principal: ManageMower,
    account_id: CurrentAccountId,
    services: ServicesDep,
) -> LinkOut:
    """Create or replace the mower's Bluetooth bridge link (``can_manage_mowers``, hidden
    otherwise): the bridge's host, port and API encryption key, the mower's Bluetooth address and
    name (from the scan) and the blade settings for manual driving. The options are validated
    with the ``ble_bridge`` schema of ``GET /connection-kinds``; the key is stored encrypted and
    never returned (``***``; sending ``***`` keeps it). Audits ``mower.link_set`` and publishes
    ``mower.updated`` with the full registration, so the Device-Gateway opens the link."""
    link = await services.links.set(
        actor_of(principal), account_id, mower_id, LINK_BLE_BRIDGE, body.to_options()
    )
    return LinkOut.of(link)


@router.get(
    "/mowers/{mower_id}/links/ble_bridge",
    responses={404: {"description": "The mower is not visible or has no bridge link."}},
)
async def get_bridge_link(
    mower_id: UUID, _: ViewMower, account_id: CurrentAccountId, services: ServicesDep
) -> LinkOut:
    """The mower's Bluetooth bridge link (``can_view``, hidden otherwise): its options with the
    key masked and the cached bridge information of the last scan. The live link state comes
    with telemetry (``MowerState.links.ble_bridge``)."""
    return LinkOut.of(await services.links.get(account_id, mower_id, LINK_BLE_BRIDGE))


@router.delete(
    "/mowers/{mower_id}/links/ble_bridge",
    status_code=204,
    responses={
        403: {"description": "The caller sees the mower but has no `can_manage_mowers`."},
        404: {"description": "The mower is not visible or has no bridge link."},
    },
)
async def delete_bridge_link(
    mower_id: UUID, principal: ManageMower, account_id: CurrentAccountId, services: ServicesDep
) -> None:
    """Remove the mower's Bluetooth bridge link (``can_manage_mowers``, hidden otherwise).
    Audits ``mower.link_removed`` and publishes ``mower.updated`` without the link, so the
    Device-Gateway closes it."""
    await services.links.delete(actor_of(principal), account_id, mower_id, LINK_BLE_BRIDGE)


@router.post(
    "/mowers/{mower_id}/links/ble_bridge/check",
    responses={
        **_SCAN_RESPONSES,
        404: {"description": "The mower is not visible or has no bridge link."},
    },
)
async def check_bridge_link(
    mower_id: UUID,
    _: ManageMower,
    account_id: CurrentAccountId,
    services: ServicesDep,
    response: Response,
) -> BridgeScanReply:
    """Scan again through the stored link ("re-check", ``can_manage_mowers``, hidden otherwise):
    the stored host, port and key; the reply is as for the scan and the link's ``proxy_info``
    (bridge firmware, outdated flag) is refreshed. While the Device-Gateway holds the link open
    it answers from the live scanner."""
    response.headers["Cache-Control"] = "no-store"
    return await services.links.check(account_id, mower_id, LINK_BLE_BRIDGE)


@router.post(
    "/mowers/{mower_id}/firmware/check",
    responses={
        404: {"description": "The mower does not exist or is not visible to the caller."},
        409: {
            "description": "`firmware-check-unsupported`: the mower's connection cannot ask the "
            "vendor (no cloud link)."
        },
        503: {
            "description": "The mower is offline, the vendor did not answer, or no Device-Gateway "
            "answered in time."
        },
    },
)
async def check_firmware(
    mower_id: UUID,
    _: ViewMower,
    account_id: CurrentAccountId,
    services: ServicesDep,
    response: Response,
) -> FirmwareInfo:
    """Ask the vendor for firmware updates now (``can_view``, hidden otherwise; ADR 0038). Fleet
    relays the request to the Device-Gateway that holds the mower, which asks through the
    mower's connection and answers the fresh ``FirmwareInfo`` (installed and available version,
    release notes, update state); the same information arrives with telemetry as
    ``MowerState.firmware_info``. Installing is the command ``firmware_update``
    (``can_manage_mowers``)."""
    response.headers["Cache-Control"] = "no-store"
    return await services.links.firmware_check(account_id, mower_id)


# --- drive calibration (ADR 0042) ----------------------------------------------------------

_CALIBRATION_RESPONSES: dict[int | str, dict[str, Any]] = {
    403: {"description": "The caller sees the mower but has no `can_drive`."},
    404: {"description": "The mower does not exist or is not visible to the caller."},
}


@router.put(
    "/mowers/{mower_id}/drive-calibration",
    responses={
        **_CALIBRATION_RESPONSES,
        409: {
            "description": "`drive-calibration-not-applicable`: the mower has no `ble_bridge` "
            "link and its connection options take no calibration."
        },
        422: {"description": "A value out of range."},
    },
)
async def set_drive_calibration(
    mower_id: UUID,
    body: DriveCalibration,
    principal: DriveMower,
    account_id: CurrentAccountId,
    services: ServicesDep,
) -> DriveCalibration:
    """Store the result of a drive calibration (``can_drive``, 403 when the mower is visible,
    else 404): the values a finished ``drive_calibrate`` reported in
    ``MowerState.drive_calibration.result.calibration``. Fleet keeps them as managed options
    of the mower's ``ble_bridge`` link (the link's key stays as it is), or in the connection
    options of a connection kind that takes them (the simulator), and replaces a stored
    calibration. Audits ``mower.drive_calibration_set`` and publishes ``mower.updated``, so the
    Device-Gateway hands the calibration to every spot run. Answers the stored calibration;
    the mower shows it as ``MowerOut.drive_calibration``."""
    return await services.links.set_drive_calibration(
        actor_of(principal), account_id, mower_id, body
    )


@router.delete(
    "/mowers/{mower_id}/drive-calibration", status_code=204, responses=_CALIBRATION_RESPONSES
)
async def delete_drive_calibration(
    mower_id: UUID, principal: DriveMower, account_id: CurrentAccountId, services: ServicesDep
) -> None:
    """Forget the stored drive calibration (``can_drive``, 403 when the mower is visible, else
    404); spot runs then go without one. Without a stored calibration
    nothing changes. Audits ``mower.drive_calibration_removed`` and publishes
    ``mower.updated``."""
    await services.links.remove_drive_calibration(actor_of(principal), account_id, mower_id)


@router.get("/commands/{command_id}")
async def get_command(
    command_id: UUID, actor: ActorDep, account_id: CurrentAccountId, services: ServicesDep
) -> CommandView:
    """One command record of the caller's account whose mower the caller may view (404
    otherwise)."""
    return await services.commands.get(actor, account_id, command_id)
