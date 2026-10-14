"""Routes of device-gateway under ``/api/v1/device-gateway``."""

from datetime import datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Request
from mowdeck_contracts.v1 import ConnectionKind, MowerActivity, Principal
from mowdeck_service_kit import ServiceUnavailable, require_permission
from pydantic import BaseModel

from device_gateway.application.gateway import DeviceGateway
from device_gateway.application.session import MowerSession
from device_gateway.domain.rules import ConnectionStatus
from device_gateway.module import VIEW_PERMISSION

router = APIRouter(prefix="/device-gateway", tags=["device-gateway"])

CanView = Annotated[Principal, Depends(require_permission(VIEW_PERMISSION))]


class HeldMower(BaseModel):
    """A mower held by this Device-Gateway instance."""

    mower_id: UUID
    account_id: UUID
    name: str
    connection_kind: ConnectionKind
    status: ConnectionStatus
    connected_since: datetime | None
    last_activity_at: datetime | None
    activity: MowerActivity | None
    battery_percent: int | None
    commands_in_flight: int

    @classmethod
    def of(cls, session: MowerSession) -> "HeldMower":
        """Snapshot of one session."""
        registration = session.registration
        state = session.last_state
        return cls(
            mower_id=registration.mower_id,
            account_id=registration.account_id,
            name=registration.name,
            connection_kind=registration.connection_kind,
            status=session.status,
            connected_since=session.connected_since,
            last_activity_at=session.last_activity_at,
            activity=state.activity if state else None,
            battery_percent=state.battery_percent if state else None,
            commands_in_flight=session.in_flight,
        )


class HeldMowers(BaseModel):
    """The mowers of this instance's shard."""

    shard_index: int
    shard_count: int
    mowers: list[HeldMower]


def get_gateway(request: Request) -> DeviceGateway:
    """Dependency: the gateway created at startup."""
    gateway: DeviceGateway | None = getattr(request.app.state, "gateway", None)
    if gateway is None:
        raise ServiceUnavailable("the device gateway is not running")
    return gateway


@router.get("/mowers")
async def list_mowers(
    principal: CanView, gateway: Annotated[DeviceGateway, Depends(get_gateway)]
) -> HeldMowers:
    """Mowers held by this instance with connection status and last activity.

    Principals bound to an account see only that account's mowers; operators and services
    (no account) see all mowers of the shard.
    """
    sessions = gateway.sessions()
    if principal.account_id is not None:
        sessions = [s for s in sessions if s.registration.account_id == principal.account_id]
    return HeldMowers(
        shard_index=gateway.shard.index,
        shard_count=gateway.shard.count,
        mowers=[HeldMower.of(s) for s in sorted(sessions, key=lambda s: str(s.mower_id))],
    )
