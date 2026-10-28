"""Routes for linked vendor cloud accounts under ``/api/v1/fleet/linked-accounts`` (ADR 0034).

Every route needs ``can_manage_mowers`` on the caller's account (403 otherwise); a linked
account of another account is 404. Passwords are write-only: no response ever contains one.
"""

from uuid import UUID

from fastapi import APIRouter, Response
from mowdeck_service_kit import CurrentAccountId

from fleet.adapters.api.deps import ActorDep, FreshAuth, ServicesDep, to_service_error
from fleet.adapters.api.schemas import (
    LinkedAccountDetail,
    LinkedAccountIn,
    LinkedAccountList,
    LinkedAccountOut,
    LinkedAccountSyncIn,
    LinkedDeviceList,
    LinkedDeviceOut,
    LinkedMowerFailure,
    LinkedMowersIn,
    LinkedMowersOut,
    MowerOut,
)
from fleet.application.linked_accounts import BulkItem, DeviceView, LinkedAccountView

router = APIRouter(prefix="/fleet/linked-accounts", tags=["linked-accounts"])

_RESPONSES: dict[int | str, dict[str, str]] = {
    403: {"description": "The caller lacks `can_manage_mowers` on the account."},
    404: {"description": "No such linked account in the caller's account."},
}
_CLOUD_RESPONSES: dict[int | str, dict[str, str]] = {
    **_RESPONSES,
    422: {"description": "`linked-account-login-failed`: the cloud refused the credentials."},
    429: {"description": "Too many failed logins for this e-mail (`Retry-After`)."},
    503: {
        "description": "No credential encryption key, or no Device-Gateway or vendor cloud "
        "answered."
    },
}


def _detail(view: LinkedAccountView, devices: list[DeviceView]) -> LinkedAccountDetail:
    return LinkedAccountDetail(
        **LinkedAccountOut.of(view).model_dump(),
        devices=[LinkedDeviceOut.of(d) for d in devices],
    )


@router.get("", responses=_RESPONSES)
async def list_linked_accounts(
    actor: ActorDep, account_id: CurrentAccountId, services: ServicesDep
) -> LinkedAccountList:
    """The linked Mammotion accounts of the caller's account (no passwords)."""
    found = await services.linked_accounts.visible(actor, account_id)
    return LinkedAccountList(items=[LinkedAccountOut.of(v) for v in found])


@router.post("", status_code=201, responses={**_CLOUD_RESPONSES, 409: {"description": "Linked."}})
async def create_linked_account(
    body: LinkedAccountIn,
    actor: ActorDep,
    account_id: CurrentAccountId,
    services: ServicesDep,
    response: Response,
) -> LinkedAccountDetail:
    """Link a Mammotion account: Fleet asks the Device-Gateway to log in and list the devices
    (``gateway.cloud.discover``) and stores the account with its password encrypted. Use a
    second Mammotion account the mowers are shared with: a login with the main account logs
    the phone app out."""
    view, devices = await services.linked_accounts.create(
        actor, account_id, body.email, body.password
    )
    response.headers["Location"] = f"/api/v1/fleet/linked-accounts/{view.account.id}"
    return _detail(view, devices)


@router.get("/{linked_account_id}", responses=_RESPONSES)
async def get_linked_account(
    linked_account_id: UUID, actor: ActorDep, account_id: CurrentAccountId, services: ServicesDep
) -> LinkedAccountOut:
    """One linked account."""
    return LinkedAccountOut.of(
        await services.linked_accounts.get(actor, account_id, linked_account_id)
    )


@router.delete(
    "/{linked_account_id}",
    status_code=204,
    responses={
        **_RESPONSES,
        409: {"description": "`linked-account-in-use`: mowers still connect through it."},
    },
)
async def delete_linked_account(
    linked_account_id: UUID,
    actor: ActorDep,
    _: FreshAuth,
    account_id: CurrentAccountId,
    services: ServicesDep,
) -> None:
    """Remove the link. Refused (409, the mowers in ``errors``) while mowers use it. Step-up
    (ADR 0057): 401 ``reauth-required`` without a recent confirmation, 403 for API tokens."""
    await services.linked_accounts.delete(actor, account_id, linked_account_id)


@router.post("/{linked_account_id}/sync", responses=_CLOUD_RESPONSES)
async def sync_linked_account(
    linked_account_id: UUID,
    body: LinkedAccountSyncIn,
    actor: ActorDep,
    account_id: CurrentAccountId,
    services: ServicesDep,
) -> LinkedAccountDetail:
    """Read the device list again ("Konto neu einlesen"), optionally with a new password. The
    gateway answers from the session the account's connected mowers share when there is one
    (a second login would log them out). A failure is kept in ``last_sync_error``."""
    view, devices = await services.linked_accounts.sync(
        actor, account_id, linked_account_id, body.password
    )
    return _detail(view, devices)


@router.get("/{linked_account_id}/devices", responses=_RESPONSES)
async def list_linked_devices(
    linked_account_id: UUID, actor: ActorDep, account_id: CurrentAccountId, services: ServicesDep
) -> LinkedDeviceList:
    """The devices of the last device list: name, model, IoT id, online flag, whether it is a
    mower and whether it is registered here or in another MowDeck account."""
    row, devices = await services.linked_accounts.devices(actor, account_id, linked_account_id)
    return LinkedDeviceList(
        items=[LinkedDeviceOut.of(d) for d in devices], last_synced_at=row.last_synced_at
    )


@router.post(
    "/{linked_account_id}/mowers",
    status_code=201,
    responses={
        **_RESPONSES,
        409: {"description": "`mower-already-registered` (when no device could be added)."},
        422: {"description": "Unknown device, not a mower, or a device given twice."},
    },
)
async def register_linked_mowers(
    linked_account_id: UUID,
    body: LinkedMowersIn,
    actor: ActorDep,
    account_id: CurrentAccountId,
    services: ServicesDep,
) -> LinkedMowersOut:
    """Register several mowers of the linked account at once. Each one is an ordinary
    registration (target site and groups checked, tuples, ``mower.registered``); devices that
    fail are listed in ``failed`` with the problem they would have answered. When none could be
    added the first problem is the answer."""
    created, failed = await services.mowers.register_from_linked_account(
        actor,
        account_id,
        linked_account_id,
        [
            BulkItem(
                device_name=item.device_name,
                name=item.name,
                site_id=item.site_id,
                group_ids=tuple(item.group_ids),
                serial=item.serial,
            )
            for item in body.mowers
        ],
    )
    failures = []
    for device_name, exc in failed:
        error = to_service_error(exc)
        failures.append(
            LinkedMowerFailure(
                device_name=device_name,
                status=error.status,
                type=error.type_uri,
                detail=error.detail or error.title,
            )
        )
    return LinkedMowersOut(created=[MowerOut.of(m) for m in created], failed=failures)
