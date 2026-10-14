"""Manual driving end to end on one in-memory bus: Realtime (served by uvicorn), Fleet (records
the session) and the Device-Gateway with a simulated mower (executes the frames).

The mower moves while frames arrive and stops by the controller's dead-man timer about one
second after the last frame, although the client never sent ``drive_stop``; closing the socket
then closes Fleet's record with ``socket_closed``.
"""

import asyncio
import time
from collections.abc import Sequence
from contextlib import AsyncExitStack
from typing import Any
from uuid import UUID

from mowdeck_auth.testing import bearer
from mowdeck_contracts.v1 import MowerRegistration
from mowdeck_control import MowerConnection, SimulatedMower
from mowdeck_service_kit.testing import lifespan_client
from realtime_support import harness

from device_gateway.adapters.control.connections import RegistryConnectionProvider
from device_gateway.application.ports import BackgroundJob
from device_gateway.main import build_app as build_gateway
from device_gateway.settings import Settings as GatewaySettings
from fleet.adapters.memory import MemoryStore as FleetStore
from fleet.main import build_app as build_fleet
from fleet.settings import Settings as FleetSettings

FLEET = "/api/v1/fleet"


class RecordingProvider(RegistryConnectionProvider):
    """The gateway's real provider, remembering the simulated mowers it created."""

    def __init__(self) -> None:
        super().__init__(sim_time_scale=1.0, sim_tick_seconds=0.05)
        self.mowers: dict[UUID, SimulatedMower] = {}

    def create(self, registration: MowerRegistration) -> MowerConnection:
        connection = super().create(registration)
        assert isinstance(connection, SimulatedMower)
        self.mowers[registration.mower_id] = connection
        return connection

    def background_jobs(self, connection: MowerConnection) -> Sequence[BackgroundJob]:
        return super().background_jobs(connection)


async def _until(condition: Any, timeout: float = 5.0) -> None:  # noqa: ASYNC109
    async with asyncio.timeout(timeout):
        while not condition():  # noqa: ASYNC110 - polling plain state of the fakes
            await asyncio.sleep(0.02)


async def test_realtime_drive_flow_moves_the_mower_and_the_dead_man_stops_it() -> None:
    async with harness() as h, AsyncExitStack() as stack:
        fleet_app = build_fleet(
            FleetSettings.model_validate(
                {"environment": "test", "log_format": "console", "nats_enabled": False}
            ),
            bus=h.bus,
            store=FleetStore(),
            authorizer=h.authz,
            tuple_backend=h.authz,
            principal_resolver=h.keys.resolver(),
            background_tasks=False,
        )
        fleet = await stack.enter_async_context(lifespan_client(fleet_app))
        provider = RecordingProvider()
        gateway_app = build_gateway(
            GatewaySettings.model_validate(
                {
                    "environment": "test",
                    "log_format": "console",
                    "telemetry_heartbeat_seconds": 5.0,
                    "connection_check_seconds": 0.05,
                    "fleet_request_timeout_seconds": 0.5,
                    "shutdown_grace_seconds": 5.0,
                }
            ),
            bus=h.bus,
            connection_provider=provider,
        )
        await stack.enter_async_context(lifespan_client(gateway_app))
        owner = await h.owner()
        created = await fleet.post(
            f"{FLEET}/mowers",
            headers=bearer(owner.token),
            json={"name": "Joystick", "connection_kind": "simulated"},
        )
        assert created.status_code == 201, created.text
        mower_id = UUID(created.json()["id"])
        hub = h.app.state.hub
        await _until(lambda: hub.capabilities(mower_id, owner.account_id) is not None)
        mower = provider.mowers[mower_id]

        client = await h.connect_as(owner)
        await client.subscribe(mower_id)
        await client.send({"type": "drive_start", "request_id": "go", "mower_id": str(mower_id)})
        started = await client.expect("drive_started", request_id="go")
        session_id = started["session_id"]
        start_position = mower.local_position

        # Ten frames per second for 1.5 s: straight ahead.
        for seq in range(15):
            await client.send(
                {
                    "type": "drive_frame",
                    "session_id": session_id,
                    "seq": seq,
                    "linear": 1.0,
                    "angular": 0.0,
                }
            )
            await asyncio.sleep(0.1)
        last_frame = time.monotonic()
        moved = mower.local_position
        assert moved != start_position
        assert abs(moved[0] - start_position[0]) + abs(moved[1] - start_position[1]) > 0.2
        # Live states show the movement to the subscribed client.
        state = await client.expect("state", timeout=3)
        assert state["state"]["mower_id"] == str(mower_id)

        # No more frames and no drive_stop: the dead-man timer ends the drive.
        await _until(lambda: mower._drive is None, timeout=3)
        stopped_after = time.monotonic() - last_frame
        assert 0.7 <= stopped_after <= 1.6, stopped_after
        resting = mower.local_position
        await asyncio.sleep(0.4)
        assert mower.local_position == resting  # it stays where it stopped

        # Fleet recorded the open session; closing the socket closes it.
        listed = await fleet.get(
            f"{FLEET}/mowers/{mower_id}/drive-sessions", headers=bearer(owner.token)
        )
        [record] = listed.json()["items"]
        assert record["session_id"] == session_id
        assert record["ended_at"] is None
        await client.ws.close()

        async def closed() -> dict[str, Any]:
            response = await fleet.get(
                f"{FLEET}/mowers/{mower_id}/drive-sessions", headers=bearer(owner.token)
            )
            item: dict[str, Any] = response.json()["items"][0]
            return item

        async with asyncio.timeout(5):
            while (await closed())["ended_at"] is None:  # noqa: ASYNC110 - polling the API
                await asyncio.sleep(0.05)
        final = await closed()
        assert final["end_reason"] == "socket_closed"
        assert final["frames"] == 15
        assert hub.drive_count == 0


async def test_realtime_drive_flow_cross_account_is_denied() -> None:
    async with harness() as h, AsyncExitStack() as stack:
        fleet_app = build_fleet(
            FleetSettings.model_validate(
                {"environment": "test", "log_format": "console", "nats_enabled": False}
            ),
            bus=h.bus,
            store=FleetStore(),
            authorizer=h.authz,
            tuple_backend=h.authz,
            principal_resolver=h.keys.resolver(),
            background_tasks=False,
        )
        fleet = await stack.enter_async_context(lifespan_client(fleet_app))
        owner = await h.owner()
        stranger = await h.owner()
        created = await fleet.post(
            f"{FLEET}/mowers",
            headers=bearer(owner.token),
            json={"name": "Private", "connection_kind": "simulated"},
        )
        mower_id = UUID(created.json()["id"])
        client = await h.connect_as(stranger)
        await client.send({"type": "drive_start", "request_id": "x", "mower_id": str(mower_id)})
        assert (await client.expect("error", request_id="x"))["code"] == "drive_not_allowed"
        listed = await fleet.get(
            f"{FLEET}/mowers/{mower_id}/drive-sessions", headers=bearer(owner.token)
        )
        assert listed.json()["items"] == []
