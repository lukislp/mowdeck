"""Startup and graceful shutdown of the gateway with an in-memory bus."""

import pytest
from dg_support import (
    SlowMower,
    TestProvider,
    connection_events,
    envelope,
    make_app,
    make_settings,
    registration,
    result_for,
    telemetry,
    wait_until,
)
from mowdeck_contracts.v1 import (
    MOWER_DISCONNECTED,
    CommandStatus,
    MowerRegistration,
    StartMow,
)
from mowdeck_control import MowerConnection
from mowdeck_service_kit.bus import InMemoryBus, publish_command
from mowdeck_service_kit.testing import lifespan_client

from device_gateway.application.gateway import GatewayDraining
from device_gateway.main import build_app


async def test_startup_and_shutdown_with_in_memory_bus() -> None:
    bus = InMemoryBus()
    reg = registration()
    app = make_app(bus=bus, bootstrap_mowers=[reg.model_dump(mode="json")])
    async with lifespan_client(app) as client:
        assert (await client.get("/readyz")).status_code == 200
        await wait_until(lambda: telemetry(bus, reg.mower_id))
    assert app.state.draining is True
    assert not app.state.commands.active
    assert app.state.gateway.sessions() == []
    assert [e.mower_id for e in connection_events(bus, MOWER_DISCONNECTED)] == [reg.mower_id]
    assert not bus.is_connected  # the kit closes the bus after the shutdown hooks


async def test_shutdown_publishes_the_result_of_an_in_flight_command() -> None:
    bus = InMemoryBus()
    reg = registration()

    def slow_mower(r: MowerRegistration) -> MowerConnection:
        return SlowMower(mower_id=r.mower_id, account_id=r.account_id, delay=0.3)

    app = make_app(
        bus=bus,
        provider=TestProvider({reg.mower_id: slow_mower}),
        bootstrap_mowers=[reg.model_dump(mode="json")],
    )
    command = envelope(reg, StartMow(area_ids=["front"]))
    async with lifespan_client(app):
        await wait_until(lambda: telemetry(bus, reg.mower_id))
        await publish_command(bus, command)
        assert result_for(bus, command) is None  # still executing when shutdown begins
    result = result_for(bus, command)
    assert result is not None
    assert result.status is CommandStatus.EXECUTED


async def test_draining_gateway_refuses_new_commands() -> None:
    bus = InMemoryBus()
    reg = registration()
    app = make_app(bus=bus, bootstrap_mowers=[reg.model_dump(mode="json")])
    async with lifespan_client(app) as client:
        gateway = app.state.gateway
        gateway.stop_accepting()
        assert (await client.get("/readyz")).status_code == 503
        with pytest.raises(GatewayDraining):
            await gateway.submit(envelope(reg, StartMow(area_ids=["front"])))
        await publish_command(bus, envelope(reg, StartMow(area_ids=["front"])))
        assert len(bus.dropped) == 1  # nak until max_deliver: redelivered after a restart


async def test_gateway_needs_the_bus() -> None:
    app = build_app(make_settings(nats_enabled=False))
    with pytest.raises(RuntimeError, match="needs the event bus"):
        async with lifespan_client(app):
            pass
