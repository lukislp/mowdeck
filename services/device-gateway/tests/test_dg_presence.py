"""Demo sandboxes (ADR 0076): the simulated mowers of an account nobody watches rest.

The presence bookkeeping with a fake clock, and the gateway with an in-memory bus: a demo gateway
starts unwatched mowers resting (no simulation clock, no states), wakes them when Realtime
announces a watcher or a command arrives, and lets them rest again when the last watcher left.
Outside demo mode every mower always runs.
"""

import asyncio
from datetime import UTC, datetime
from uuid import UUID, uuid4

from dg_support import (
    TestProvider,
    envelope,
    make_app,
    registration,
    result_for,
    telemetry,
    wait_until,
)
from mowdeck_contracts.v1 import (
    ACCOUNT_PRESENCE,
    AccountPresence,
    AccountWatchers,
    CommandStatus,
    StartMow,
)
from mowdeck_control import SimulatedMower
from mowdeck_service_kit.bus import InMemoryBus, publish_command
from mowdeck_service_kit.testing import lifespan_client

from device_gateway.application.presence import PresenceTracker


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def _presence(
    instance: str, counts: dict[UUID, int], *, full: bool = False, interval: float = 30.0
) -> AccountPresence:
    return AccountPresence(
        instance=instance,
        accounts=[AccountWatchers(account_id=a, watchers=n) for a, n in counts.items()],
        full=full,
        interval_seconds=interval,
        at=datetime.now(UTC),
    )


# --- the bookkeeping ---------------------------------------------------------------------------


def test_an_account_is_awake_while_a_realtime_instance_reports_a_watcher() -> None:
    clock, anna = FakeClock(), uuid4()
    tracker = PresenceTracker(clock=clock)
    assert not tracker.awake(anna)
    assert tracker.update(_presence("rt-1", {anna: 1})) == {anna}
    assert tracker.awake(anna)
    assert tracker.update(_presence("rt-1", {anna: 2})) == set()  # no change
    assert tracker.update(_presence("rt-1", {anna: 0})) == {anna}
    assert not tracker.awake(anna)


def test_watchers_on_two_instances_count_separately() -> None:
    clock, anna = FakeClock(), uuid4()
    tracker = PresenceTracker(clock=clock)
    tracker.update(_presence("rt-1", {anna: 1}))
    tracker.update(_presence("rt-2", {anna: 1}))
    assert tracker.update(_presence("rt-1", {anna: 0})) == set()
    assert tracker.awake(anna)  # still watched through the other instance
    assert tracker.update(_presence("rt-2", {anna: 0})) == {anna}


def test_a_keep_alive_without_the_account_means_nobody_watches_it_there() -> None:
    clock, anna, ben = FakeClock(), uuid4(), uuid4()
    tracker = PresenceTracker(clock=clock)
    tracker.update(_presence("rt-1", {anna: 1, ben: 1}))
    changed = tracker.update(_presence("rt-1", {ben: 1}, full=True))
    assert changed == {anna}
    assert tracker.awake(ben)


def test_reports_of_a_dead_instance_time_out() -> None:
    clock, anna = FakeClock(), uuid4()
    tracker = PresenceTracker(clock=clock, grace=2.5)
    tracker.update(_presence("rt-1", {anna: 1}, interval=30.0))
    clock.now += 74
    assert tracker.expire() == set()
    clock.now += 2
    assert tracker.expire() == {anna}
    assert not tracker.awake(anna)


def test_a_request_wakes_an_account_for_a_while() -> None:
    clock, anna = FakeClock(), uuid4()
    tracker = PresenceTracker(clock=clock, wake_seconds=120)
    assert tracker.wake(anna) == {anna}
    assert tracker.wake(anna) == set()
    clock.now += 119
    assert tracker.expire() == set()
    clock.now += 2
    assert tracker.expire() == {anna}


def test_forgetting_an_account_drops_everything() -> None:
    clock, anna = FakeClock(), uuid4()
    tracker = PresenceTracker(clock=clock)
    tracker.update(_presence("rt-1", {anna: 1}))
    tracker.forget(anna)
    assert not tracker.awake(anna)
    assert tracker.awake_accounts() == frozenset()


# --- the gateway ---------------------------------------------------------------------------------


def _elapsed(provider: TestProvider, mower_id: UUID) -> float:
    connection = provider.latest(mower_id)
    assert isinstance(connection, SimulatedMower)
    return float(connection.snapshot().raw["elapsed_s"])


async def _connected(provider: TestProvider, mower_id: UUID) -> None:
    await wait_until(lambda: mower_id in provider.created)
    connection = provider.latest(mower_id)
    await wait_until(lambda: connection.is_connected)


async def test_a_demo_gateway_lets_unwatched_mowers_rest_and_wakes_them_for_a_watcher() -> None:
    bus, provider = InMemoryBus(), TestProvider()
    reg = registration()
    app = make_app(
        bus=bus,
        provider=provider,
        demo_mode=True,
        bootstrap_mowers=[reg.model_dump(mode="json")],
    )
    async with lifespan_client(app):
        gateway = app.state.gateway
        await _connected(provider, reg.mower_id)
        await asyncio.sleep(0.3)
        assert gateway.paused_count() == 1
        assert telemetry(bus, reg.mower_id) == []
        assert _elapsed(provider, reg.mower_id) == 0.0  # simulated time stands still

        await bus.publish(ACCOUNT_PRESENCE, _presence("rt-1", {reg.account_id: 1}))
        await wait_until(lambda: telemetry(bus, reg.mower_id))
        await wait_until(lambda: _elapsed(provider, reg.mower_id) > 0)
        assert gateway.paused_count() == 0

        await bus.publish(ACCOUNT_PRESENCE, _presence("rt-1", {reg.account_id: 0}))
        await wait_until(lambda: gateway.paused_count() == 1)
        await asyncio.sleep(0.1)
        frozen, published = _elapsed(provider, reg.mower_id), len(telemetry(bus, reg.mower_id))
        await asyncio.sleep(0.3)
        assert _elapsed(provider, reg.mower_id) == frozen
        assert len(telemetry(bus, reg.mower_id)) == published


async def test_a_command_wakes_a_resting_mower_and_is_executed() -> None:
    bus, provider = InMemoryBus(), TestProvider()
    reg = registration()
    app = make_app(
        bus=bus,
        provider=provider,
        demo_mode=True,
        bootstrap_mowers=[reg.model_dump(mode="json")],
    )
    command = envelope(reg, StartMow(area_ids=["front"]))
    async with lifespan_client(app):
        await _connected(provider, reg.mower_id)
        assert app.state.gateway.paused_count() == 1
        await publish_command(bus, command)
        await wait_until(lambda: result_for(bus, command) is not None)
        await wait_until(lambda: telemetry(bus, reg.mower_id))
        assert app.state.gateway.paused_count() == 0
    result = result_for(bus, command)
    assert result is not None
    assert result.status is not CommandStatus.REJECTED


async def test_watchers_of_one_account_do_not_wake_another() -> None:
    bus, provider = InMemoryBus(), TestProvider()
    anna, ben = registration(), registration()
    app = make_app(
        bus=bus,
        provider=provider,
        demo_mode=True,
        bootstrap_mowers=[anna.model_dump(mode="json"), ben.model_dump(mode="json")],
    )
    async with lifespan_client(app):
        await _connected(provider, anna.mower_id)
        await _connected(provider, ben.mower_id)
        await bus.publish(ACCOUNT_PRESENCE, _presence("rt-1", {anna.account_id: 1}))
        await wait_until(lambda: telemetry(bus, anna.mower_id))
        await asyncio.sleep(0.2)
        assert telemetry(bus, ben.mower_id) == []
        assert app.state.gateway.session(ben.mower_id).paused


async def test_outside_demo_mode_every_mower_runs_without_watchers() -> None:
    bus, provider = InMemoryBus(), TestProvider()
    reg = registration()
    app = make_app(bus=bus, provider=provider, bootstrap_mowers=[reg.model_dump(mode="json")])
    async with lifespan_client(app):
        await wait_until(lambda: telemetry(bus, reg.mower_id))
        assert app.state.gateway.paused_count() == 0
        assert getattr(app.state, "presence", None) is None


async def test_a_demo_gateway_can_switch_the_pause_off() -> None:
    bus, provider = InMemoryBus(), TestProvider()
    reg = registration()
    app = make_app(
        bus=bus,
        provider=provider,
        demo_mode=True,
        sim_pause_unwatched=False,
        bootstrap_mowers=[reg.model_dump(mode="json")],
    )
    async with lifespan_client(app):
        await wait_until(lambda: telemetry(bus, reg.mower_id))
        assert app.state.gateway.paused_count() == 0
