"""Routes for RTK base stations under ``/api/v1/fleet/base-stations`` (ADR 0047).

Reading (list, get, firmware check) needs ``can_view`` on the caller's account, every change
``can_manage_mowers`` on it (403 otherwise); a base station of another account is 404. Linking a
mower is ``PUT /api/v1/fleet/mowers/{mower_id}/base-station`` (``can_manage_mowers`` on the
mower). The live state of a base station is in the telemetry of its linked mowers
(``MowerState.base_station``).
"""

from uuid import UUID

from fastapi import APIRouter, Response
from mowdeck_contracts.v1 import CommandAccepted, FirmwareInfo
from mowdeck_service_kit import CurrentAccountId

from fleet.adapters.api.deps import ActorDep, ManageMower, ServicesDep, actor_of
from fleet.adapters.api.schemas import (
    BaseStationFirmwareUpdateIn,
    BaseStationIn,
    BaseStationList,
    BaseStationOut,
    BaseStationPatch,
    MowerBaseStationIn,
    MowerOut,
)
from fleet.application.base_stations import BaseStationInput

router = APIRouter(prefix="/fleet", tags=["base-stations"])

_RESPONSES: dict[int | str, dict[str, str]] = {
    403: {"description": "The caller lacks the relation on the account."},
    404: {"description": "No such base station in the caller's account."},
}


@router.get("/base-stations", responses=_RESPONSES)
async def list_base_stations(
    actor: ActorDep, account_id: CurrentAccountId, services: ServicesDep
) -> BaseStationList:
    """The RTK base stations of the caller's account with their linked mowers (``can_view``)."""
    found = await services.base_stations.visible(actor, account_id)
    return BaseStationList(items=[BaseStationOut.of(v) for v in found])


@router.post(
    "/base-stations",
    status_code=201,
    responses={
        **_RESPONSES,
        409: {"description": "`base-station-already-registered`, or the account's limit."},
        422: {"description": "Unknown linked account or device, or not an RTK base station."},
    },
)
async def register_base_station(
    body: BaseStationIn,
    actor: ActorDep,
    account_id: CurrentAccountId,
    services: ServicesDep,
    response: Response,
) -> BaseStationOut:
    """Register an RTK base station (``can_manage_mowers``): a device of a linked account that
    its device list marks ``rtk_base``, or (``source`` ``simulated``) the simulator's base
    station. 409 ``base-station-already-registered`` when that physical base station is
    registered already (in this or another account, which is not named)."""
    created = await services.base_stations.register(
        actor,
        account_id,
        BaseStationInput(
            name=body.name,
            source=body.source,
            linked_account_id=body.linked_account_id,
            device_name=body.device_name,
        ),
    )
    response.headers["Location"] = f"/api/v1/fleet/base-stations/{created.base.id}"
    return BaseStationOut.of(created)


@router.get("/base-stations/{base_station_id}", responses=_RESPONSES)
async def get_base_station(
    base_station_id: UUID, actor: ActorDep, account_id: CurrentAccountId, services: ServicesDep
) -> BaseStationOut:
    """One base station (``can_view``)."""
    return BaseStationOut.of(await services.base_stations.get(actor, account_id, base_station_id))


@router.patch("/base-stations/{base_station_id}", responses=_RESPONSES)
async def update_base_station(
    base_station_id: UUID,
    body: BaseStationPatch,
    actor: ActorDep,
    account_id: CurrentAccountId,
    services: ServicesDep,
) -> BaseStationOut:
    """Rename the base station (``can_manage_mowers``)."""
    found = await services.base_stations.rename(actor, account_id, base_station_id, body.name)
    return BaseStationOut.of(found)


@router.delete("/base-stations/{base_station_id}", status_code=204, responses=_RESPONSES)
async def delete_base_station(
    base_station_id: UUID, actor: ActorDep, account_id: CurrentAccountId, services: ServicesDep
) -> None:
    """Remove the base station (``can_manage_mowers``); its mowers lose the link."""
    await services.base_stations.delete(actor, account_id, base_station_id)


@router.post(
    "/base-stations/{base_station_id}/firmware/check",
    responses={
        **_RESPONSES,
        409: {"description": "`base-station-not-linked`: no mower is linked to it."},
        503: {"description": "No Device-Gateway holding a linked mower answered."},
    },
)
async def check_base_station_firmware(
    base_station_id: UUID, actor: ActorDep, account_id: CurrentAccountId, services: ServicesDep
) -> FirmwareInfo:
    """Ask the vendor for the base station's firmware through a linked mower (``can_view``);
    the answer also lands in the linked mowers' state (`MowerState.base_station`)."""
    return await services.base_stations.firmware_check(actor, account_id, base_station_id)


@router.post(
    "/base-stations/{base_station_id}/firmware/update",
    status_code=202,
    responses={
        **_RESPONSES,
        409: {"description": "`base-station-not-linked`: no mower is linked to it."},
        422: {"description": "`mower_id` is not linked to the base station."},
    },
)
async def update_base_station_firmware(
    base_station_id: UUID,
    body: BaseStationFirmwareUpdateIn,
    actor: ActorDep,
    account_id: CurrentAccountId,
    services: ServicesDep,
) -> CommandAccepted:
    """Install the offered firmware (``can_manage_mowers`` and the plan feature ``control``):
    the command ``base_station_firmware_update`` goes to a linked mower, which relays it to the
    vendor; follow it at ``status_url``. The mower refuses it while it mows, returns, updates
    or drives, or when the version is not the offered one."""
    return await services.base_stations.firmware_update(
        actor, account_id, base_station_id, body.version, body.mower_id
    )


@router.put(
    "/mowers/{mower_id}/base-station",
    tags=["mowers"],
    responses={
        403: {"description": "The caller sees the mower but has no `can_manage_mowers`."},
        404: {"description": "The mower or the base station does not exist in the account."},
        409: {"description": "`base-station-not-applicable`: it cannot serve this mower."},
    },
)
async def set_mower_base_station(
    mower_id: UUID,
    body: MowerBaseStationIn,
    principal: ManageMower,
    account_id: CurrentAccountId,
    services: ServicesDep,
) -> MowerOut:
    """Link the mower to an RTK base station of the account, or unlink it with ``null``
    (``can_manage_mowers`` on the mower). A cloud base station serves cloud mowers of its own
    linked account, the simulated one simulated mowers. Publishes ``mower.updated``: the
    Device-Gateway reconnects the mower with the base station."""
    await services.base_stations.link_mower(
        actor_of(principal), account_id, mower_id, body.base_station_id
    )
    return MowerOut.of(await services.mowers.get(account_id, mower_id))
