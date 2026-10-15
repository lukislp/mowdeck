"""Asks Fleet for the registrations of this shard and for the secrets it withholds from them
(core NATS request/reply)."""

from mowdeck_contracts.v1 import (
    MowerCredentialsReply,
    MowerCredentialsRequest,
    MowerListReply,
    MowerListRequest,
    MowerRegistration,
)
from mowdeck_contracts.v1.subjects import FLEET_MOWERS_CREDENTIALS, FLEET_MOWERS_LIST
from mowdeck_service_kit.bus import EventBus

from device_gateway.application.ports import MowerCredentials
from device_gateway.domain.rules import Shard


class BusFleetDirectory:
    """:class:`~device_gateway.application.ports.FleetDirectory` over ``fleet.mowers.list``."""

    def __init__(self, bus: EventBus, *, timeout_seconds: float) -> None:
        self._bus = bus
        self._timeout = timeout_seconds

    async def list_mowers(self, shard: Shard) -> list[MowerRegistration]:
        """Request the shard's mowers; raises the bus errors (no responders, timeout)."""
        request = MowerListRequest(shard_index=shard.index, shard_count=shard.count)
        reply = await self._bus.request(FLEET_MOWERS_LIST, request, self._timeout)
        return reply.model(MowerListReply).mowers


class BusCredentialSource:
    """:class:`~device_gateway.application.ports.CredentialSource` over
    ``fleet.mowers.credentials`` (ADR 0034). The reply is used for one connect and never
    logged."""

    def __init__(self, bus: EventBus, *, timeout_seconds: float) -> None:
        self._bus = bus
        self._timeout = timeout_seconds

    async def fetch(self, registration: MowerRegistration) -> MowerCredentials:
        """The withheld options of the connection and its links; ``LookupError`` when Fleet
        holds none for the mower."""
        request = MowerCredentialsRequest(
            mower_id=registration.mower_id, account_id=registration.account_id
        )
        reply = await self._bus.request(FLEET_MOWERS_CREDENTIALS, request, self._timeout)
        answer = reply.model(MowerCredentialsReply)
        if not answer.found:
            msg = "Fleet holds no credentials for this mower"
            raise LookupError(msg)
        return MowerCredentials(
            options=dict(answer.secret_options),
            link_secrets={k: dict(v) for k, v in answer.link_secrets.items()},
        )
