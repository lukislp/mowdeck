"""Commands: execution, expiry, duplicates, per-mower concurrency, tenancy and sharding."""

import time
from datetime import timedelta
from uuid import uuid4

from dg_support import (
    FailingConnection,
    SlowMower,
    TestProvider,
    envelope,
    make_app,
    mower_id_in_shard,
    registration,
    result_for,
    results,
    telemetry,
    wait_until,
)
from mowdeck_contracts.v1 import (
    CommandStatus,
    EmergencyStop,
    MowerActivity,
    MowerRegistration,
    StartMow,
    utc_now,
)
from mowdeck_contracts.v1.subjects import command_subject
from mowdeck_control import MowerConnection
from mowdeck_service_kit.bus import InMemoryBus, publish_command
from mowdeck_service_kit.testing import lifespan_client

START = StartMow(area_ids=["front"])


async def test_start_command_is_executed_and_telemetry_shows_mowing() -> None:
    bus = InMemoryBus()
    reg = registration()
    app = make_app(bus=bus, bootstrap_mowers=[reg.model_dump(mode="json")])
    async with lifespan_client(app):
        await wait_until(lambda: telemetry(bus, reg.mower_id))
        command = envelope(reg, START)
        await publish_command(bus, command)
        await wait_until(lambda: result_for(bus, command))
        result = result_for(bus, command)
        assert result is not None
        assert result.status is CommandStatus.EXECUTED
        assert result.correlation_id == command.correlation_id
        await wait_until(
            lambda: any(s.activity is MowerActivity.MOWING for s in telemetry(bus, reg.mower_id))
        )


async def test_expired_command_is_answered_with_expired() -> None:
    bus = InMemoryBus()
    reg = registration()
    provider = TestProvider()
    app = make_app(bus=bus, provider=provider, bootstrap_mowers=[reg.model_dump(mode="json")])
    async with lifespan_client(app):
        await wait_until(lambda: telemetry(bus, reg.mower_id))
        stale = envelope(reg, START, ttl=timedelta(seconds=1), now=utc_now() - timedelta(minutes=5))
        await bus.publish(command_subject(reg.mower_id), stale, msg_id=str(stale.command_id))
        await wait_until(lambda: result_for(bus, stale))
        result = result_for(bus, stale)
        assert result is not None
        assert result.status is CommandStatus.EXPIRED
        mower = provider.latest(reg.mower_id)
        assert isinstance(mower, SlowMower)
        assert mower.executed == []


async def test_duplicate_command_yields_one_result() -> None:
    bus = InMemoryBus()
    reg = registration()
    provider = TestProvider()
    app = make_app(bus=bus, provider=provider, bootstrap_mowers=[reg.model_dump(mode="json")])
    async with lifespan_client(app):
        await wait_until(lambda: telemetry(bus, reg.mower_id))
        command = envelope(reg, START)
        await publish_command(bus, command)
        duplicate = await publish_command(bus, command)
        assert duplicate.duplicate  # JetStream de-duplication by command id
        # A redelivery (e.g. after a missing ack) reaches the gateway again.
        await bus.publish(command_subject(reg.mower_id), command, msg_id=f"redelivery-{uuid4()}")
        await wait_until(lambda: result_for(bus, command))
        await app.state.gateway.drain()
        assert [r.command_id for r in results(bus, reg.mower_id)] == [command.command_id]
        mower = provider.latest(reg.mower_id)
        assert isinstance(mower, SlowMower)
        assert mower.executed == ["start_mow"]


async def test_emergency_stop_is_not_delayed_by_another_mowers_slow_command() -> None:
    bus = InMemoryBus()
    fast, slow = registration(name="A"), registration(name="B")

    def slow_mower(reg: MowerRegistration) -> MowerConnection:
        return SlowMower(mower_id=reg.mower_id, account_id=reg.account_id, delay=1.5)

    provider = TestProvider({slow.mower_id: slow_mower})
    app = make_app(
        bus=bus,
        provider=provider,
        bootstrap_mowers=[fast.model_dump(mode="json"), slow.model_dump(mode="json")],
    )
    async with lifespan_client(app):
        await wait_until(lambda: telemetry(bus, fast.mower_id) and telemetry(bus, slow.mower_id))
        slow_start = envelope(slow, START)
        await publish_command(bus, slow_start)
        stop = envelope(fast, EmergencyStop())
        started = time.monotonic()
        await publish_command(bus, stop)
        await wait_until(lambda: result_for(bus, stop), within=1.0)
        elapsed = time.monotonic() - started
        assert elapsed < 0.5
        assert result_for(bus, slow_start) is None  # still running
        result = result_for(bus, stop)
        assert result is not None
        assert result.status is CommandStatus.EXECUTED
        await wait_until(lambda: result_for(bus, slow_start))


async def test_emergency_stop_overtakes_a_slow_command_of_the_same_mower() -> None:
    bus = InMemoryBus()
    reg = registration()

    def slow_mower(r: MowerRegistration) -> MowerConnection:
        return SlowMower(mower_id=r.mower_id, account_id=r.account_id, delay=1.0)

    app = make_app(
        bus=bus,
        provider=TestProvider({reg.mower_id: slow_mower}),
        bootstrap_mowers=[reg.model_dump(mode="json")],
    )
    async with lifespan_client(app):
        await wait_until(lambda: telemetry(bus, reg.mower_id))
        await publish_command(bus, envelope(reg, START))
        stop = envelope(reg, EmergencyStop())
        started = time.monotonic()
        await publish_command(bus, stop)
        await wait_until(lambda: result_for(bus, stop), within=0.8)
        assert time.monotonic() - started < 0.5


async def test_command_of_a_foreign_account_is_rejected_before_the_controller() -> None:
    bus = InMemoryBus()
    reg = registration()
    provider = TestProvider()
    app = make_app(bus=bus, provider=provider, bootstrap_mowers=[reg.model_dump(mode="json")])
    async with lifespan_client(app):
        await wait_until(lambda: telemetry(bus, reg.mower_id))
        intruder = envelope(reg, START, account_id=uuid4())
        await publish_command(bus, intruder)
        result = result_for(bus, intruder)
        assert result is not None
        assert result.status is CommandStatus.REJECTED
        assert result.reason == "the command's account does not own this mower"
        mower = provider.latest(reg.mower_id)
        assert isinstance(mower, SlowMower)
        assert mower.executed == []
        assert all(s.activity is not MowerActivity.MOWING for s in telemetry(bus, reg.mower_id))


async def test_command_for_an_unknown_mower_of_this_shard_is_rejected() -> None:
    bus = InMemoryBus()
    app = make_app(bus=bus)
    async with lifespan_client(app):
        command = envelope(registration(), START)
        await publish_command(bus, command)
        result = result_for(bus, command)
        assert result is not None
        assert (result.status, result.reason) == (CommandStatus.REJECTED, "unknown mower")


async def test_commands_outside_the_shard_are_acked_and_ignored() -> None:
    bus = InMemoryBus()
    mine = registration(mower_id_in_shard(1, 2))
    foreign = registration(mower_id_in_shard(0, 2))
    app = make_app(
        bus=bus,
        shard_index=1,
        shard_count=2,
        bootstrap_mowers=[mine.model_dump(mode="json"), foreign.model_dump(mode="json")],
    )
    async with lifespan_client(app):
        gateway = app.state.gateway
        assert [s.mower_id for s in gateway.sessions()] == [mine.mower_id]
        assert app.state.commands.active
        await wait_until(lambda: telemetry(bus, mine.mower_id))
        ignored = envelope(foreign, START)
        handled = envelope(mine, START)
        await publish_command(bus, ignored)
        await publish_command(bus, handled)
        await wait_until(lambda: result_for(bus, handled))
        assert result_for(bus, ignored) is None
        assert bus.dropped == []  # acknowledged, not redelivered or terminated


async def test_invalid_messages_are_dropped() -> None:
    bus = InMemoryBus()
    reg = registration()
    other = registration()
    app = make_app(bus=bus, bootstrap_mowers=[reg.model_dump(mode="json")])
    async with lifespan_client(app):
        await bus.publish(command_subject(reg.mower_id), b"{not json")
        # The subject names one mower, the envelope another.
        await bus.publish(command_subject(reg.mower_id), envelope(other, START))
        assert len(bus.dropped) == 2
        assert results(bus, reg.mower_id) == []


async def test_command_while_the_mower_is_not_connected_fails() -> None:
    bus = InMemoryBus()
    reg = registration()

    def unreachable(r: MowerRegistration) -> MowerConnection:
        return FailingConnection(mower_id=r.mower_id, account_id=r.account_id)

    provider = TestProvider({reg.mower_id: unreachable})
    app = make_app(bus=bus, provider=provider, bootstrap_mowers=[reg.model_dump(mode="json")])
    async with lifespan_client(app):
        await wait_until(lambda: len(provider.created.get(reg.mower_id, [])) >= 2)
        command = envelope(reg, START)
        await publish_command(bus, command)
        await wait_until(lambda: result_for(bus, command))
        result = result_for(bus, command)
        assert result is not None
        assert (result.status, result.reason) == (CommandStatus.FAILED, "mower is not connected")
