"""The link heartbeat (ADR 0040): ``heartbeat_at`` advances while the link is alive, the values
and their ``observed_at`` stay, and a lost link stops the heartbeats with one offline state."""

import asyncio

from dg_support import (
    ConnectionFactory,
    FailingConnection,
    SlowMower,
    TestProvider,
    connection_events,
    make_app,
    registration,
    telemetry,
    wait_until,
)
from mowdeck_contracts.v1 import MOWER_DISCONNECTED, MowerRegistration
from mowdeck_control import MowerConnection, SimulationConfig
from mowdeck_service_kit.bus import InMemoryBus
from mowdeck_service_kit.testing import lifespan_client


def _first_then_failing(provider_created: list[MowerConnection]) -> ConnectionFactory:
    """A factory: a simulated mower for the first connect, then connects that always fail."""

    def factory(reg: MowerRegistration) -> MowerConnection:
        if provider_created:
            return FailingConnection(mower_id=reg.mower_id, account_id=reg.account_id)
        mower = SlowMower(
            mower_id=reg.mower_id, account_id=reg.account_id, config=SimulationConfig(time_scale=60)
        )
        provider_created.append(mower)
        return mower

    return factory


async def test_heartbeats_advance_heartbeat_at_and_keep_observed_at() -> None:
    bus = InMemoryBus()
    reg = registration()
    app = make_app(
        bus=bus,
        provider=TestProvider(tick_seconds=None),  # no simulation ticks: no new reports
        telemetry_heartbeat_seconds=0.05,
        bootstrap_mowers=[reg.model_dump(mode="json")],
    )
    async with lifespan_client(app):
        await wait_until(lambda: len(telemetry(bus, reg.mower_id)) >= 4)
        states = telemetry(bus, reg.mower_id)
    beats = [s.heartbeat_at for s in states if s.heartbeat_at is not None]
    assert len(beats) == len(states)
    assert beats == sorted(beats) and len(set(beats)) == len(beats)  # a fresh one every time
    assert {s.observed_at for s in states} == {states[0].observed_at}  # the values stay
    assert all(s.connected for s in states)
    assert all(s.heartbeat_at is not None and s.heartbeat_at >= s.observed_at for s in states)


async def test_lost_link_stops_the_heartbeats_and_publishes_one_offline_state() -> None:
    bus = InMemoryBus()
    reg = registration()
    created: list[MowerConnection] = []
    provider = TestProvider(
        {reg.mower_id: _first_then_failing(created)},
        tick_seconds=None,
    )
    app = make_app(
        bus=bus,
        provider=provider,
        telemetry_heartbeat_seconds=0.05,
        bootstrap_mowers=[reg.model_dump(mode="json")],
    )
    async with lifespan_client(app):
        await wait_until(lambda: len(telemetry(bus, reg.mower_id)) >= 3)
        await created[0].disconnect()  # the link drops; reconnects fail from now on
        await wait_until(lambda: connection_events(bus, MOWER_DISCONNECTED))
        states = telemetry(bus, reg.mower_id)
        await asyncio.sleep(0.3)  # six heartbeat intervals
        assert len(telemetry(bus, reg.mower_id)) == len(states), "no heartbeat without a link"
    online, offline = states[:-1], states[-1]
    assert all(s.connected for s in online)
    assert offline.connected is False
    assert offline.observed_at == online[-1].observed_at  # the last values, unchanged
    assert offline.heartbeat_at == online[-1].heartbeat_at  # the last time the link was alive
    assert offline.model_dump(exclude={"connected"}) == online[-1].model_dump(exclude={"connected"})


async def test_no_heartbeat_once_the_connection_says_it_is_down() -> None:
    bus = InMemoryBus()
    reg = registration()
    provider = TestProvider(tick_seconds=None)
    app = make_app(
        bus=bus,
        provider=provider,
        telemetry_heartbeat_seconds=0.05,
        connection_check_seconds=30.0,  # the link watch notices the drop only much later
        bootstrap_mowers=[reg.model_dump(mode="json")],
    )
    async with lifespan_client(app):
        await wait_until(lambda: len(telemetry(bus, reg.mower_id)) >= 2)
        await provider.latest(reg.mower_id).disconnect()
        await asyncio.sleep(0.06)  # a heartbeat that was already on its way
        count = len(telemetry(bus, reg.mower_id))
        await asyncio.sleep(0.3)
        assert len(telemetry(bus, reg.mower_id)) == count
