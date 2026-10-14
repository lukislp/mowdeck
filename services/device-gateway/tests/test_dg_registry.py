"""The volatile registry: Fleet events, the Fleet request, reconnects and heartbeats."""

from uuid import uuid4

from dg_support import (
    TestProvider,
    connection_events,
    make_app,
    mower_id_in_shard,
    registration,
    telemetry,
    wait_until,
)
from mowdeck_contracts.v1 import (
    MOWER_CONNECTED,
    MOWER_DISCONNECTED,
    MOWER_REGISTERED,
    MOWER_REMOVED,
    MOWER_UPDATED,
    EventEnvelope,
    MowerListReply,
    MowerListRequest,
    MowerRegistration,
)
from mowdeck_contracts.v1.subjects import FLEET_MOWERS_LIST
from mowdeck_service_kit.bus import BusMessage, InMemoryBus, publish_event
from mowdeck_service_kit.testing import lifespan_client

from device_gateway.adapters.bus.fleet import BusFleetDirectory
from device_gateway.domain.rules import ConnectionStatus


def _event(event_type: str, reg: MowerRegistration) -> EventEnvelope[MowerRegistration]:
    return EventEnvelope[MowerRegistration](
        type=event_type, producer="fleet", account_id=reg.account_id, payload=reg
    )


async def test_registered_adds_and_removed_closes_a_mower_at_runtime() -> None:
    bus = InMemoryBus()
    app = make_app(bus=bus)
    async with lifespan_client(app):
        gateway = app.state.gateway
        assert gateway.sessions() == []
        reg = registration()
        await publish_event(bus, _event(MOWER_REGISTERED, reg))
        await wait_until(lambda: connection_events(bus, MOWER_CONNECTED))
        assert connection_events(bus, MOWER_CONNECTED)[0].mower_id == reg.mower_id
        await wait_until(lambda: telemetry(bus, reg.mower_id))

        await publish_event(bus, _event(MOWER_REMOVED, reg))
        assert gateway.session(reg.mower_id) is None
        disconnected = connection_events(bus, MOWER_DISCONNECTED)
        assert [d.mower_id for d in disconnected] == [reg.mower_id]
        assert disconnected[0].account_id == reg.account_id
        assert disconnected[0].reason == "gateway released the mower"


async def test_updated_renames_in_place_or_reconnects_on_a_new_link() -> None:
    bus = InMemoryBus()
    reg = registration(name="Old name")
    provider = TestProvider()
    app = make_app(bus=bus, provider=provider, bootstrap_mowers=[reg.model_dump(mode="json")])
    async with lifespan_client(app):
        gateway = app.state.gateway
        await wait_until(lambda: connection_events(bus, MOWER_CONNECTED))
        renamed = reg.model_copy(update={"name": "New name"})
        await publish_event(bus, _event(MOWER_UPDATED, renamed))
        session = gateway.session(reg.mower_id)
        assert session is not None
        assert session.registration.name == "New name"
        assert len(provider.created[reg.mower_id]) == 1

        relinked = renamed.model_copy(update={"connection_options": {"battery_percent": 50}})
        await publish_event(bus, _event(MOWER_UPDATED, relinked))
        await wait_until(lambda: len(connection_events(bus, MOWER_CONNECTED)) == 2)
        assert len(provider.created[reg.mower_id]) == 2
        assert len(connection_events(bus, MOWER_DISCONNECTED)) == 1


async def test_registry_ignores_foreign_shards_and_bad_events() -> None:
    bus = InMemoryBus()
    app = make_app(bus=bus, shard_index=0, shard_count=2)
    async with lifespan_client(app):
        foreign = registration(mower_id_in_shard(1, 2))
        await publish_event(bus, _event(MOWER_REGISTERED, foreign))
        assert app.state.gateway.sessions() == []
        await bus.publish("event.mower.registered", b"not an envelope")
        bad_payload = EventEnvelope[dict[str, str]](
            type=MOWER_REGISTERED, producer="fleet", payload={"mower_id": "nope"}
        )
        await publish_event(bus, bad_payload)
        assert len(bus.dropped) == 2
        assert not await app.state.gateway.remove(uuid4(), source="test")


async def test_startup_without_fleet_is_ready_and_keeps_retrying() -> None:
    bus = InMemoryBus()
    calls: list[BusMessage] = []

    async def silent_fleet(message: BusMessage) -> None:
        calls.append(message)  # never responds: the request times out

    await bus.subscribe(FLEET_MOWERS_LIST, silent_fleet)
    app = make_app(bus=bus, fleet=BusFleetDirectory(bus, timeout_seconds=0.05))
    async with lifespan_client(app) as client:
        response = await client.get("/readyz")
        assert response.status_code == 200
        assert response.json()["checks"]["commands"]["status"] == "ok"
        await wait_until(lambda: calls)
        assert not app.state.gateway.fleet_synced
        assert app.state.gateway.sessions() == []


async def test_fleet_reply_registers_the_mowers_of_this_shard() -> None:
    bus = InMemoryBus()
    mine = registration(mower_id_in_shard(0, 2))
    foreign = registration(mower_id_in_shard(1, 2))
    requests: list[MowerListRequest] = []

    async def fleet(message: BusMessage) -> None:
        requests.append(message.model(MowerListRequest))
        await message.respond(MowerListReply(mowers=[mine, foreign]))

    await bus.subscribe(FLEET_MOWERS_LIST, fleet)
    app = make_app(
        bus=bus,
        shard_index=0,
        shard_count=2,
        fleet=BusFleetDirectory(bus, timeout_seconds=1.0),
    )
    async with lifespan_client(app):
        gateway = app.state.gateway
        await wait_until(lambda: gateway.fleet_synced)
        assert requests == [MowerListRequest(shard_index=0, shard_count=2)]
        assert [s.mower_id for s in gateway.sessions()] == [mine.mower_id]


async def test_dropped_connection_is_reconnected() -> None:
    bus = InMemoryBus()
    reg = registration()
    provider = TestProvider()
    app = make_app(bus=bus, provider=provider, bootstrap_mowers=[reg.model_dump(mode="json")])
    async with lifespan_client(app):
        session = app.state.gateway.session(reg.mower_id)
        await wait_until(lambda: session.status is ConnectionStatus.CONNECTED)
        await provider.latest(reg.mower_id).disconnect()  # the link drops
        await wait_until(lambda: connection_events(bus, MOWER_DISCONNECTED))
        assert connection_events(bus, MOWER_DISCONNECTED)[0].reason == "connection lost"
        await wait_until(lambda: len(connection_events(bus, MOWER_CONNECTED)) == 2)
        assert session.status is ConnectionStatus.CONNECTED
        assert len(provider.created[reg.mower_id]) == 2


async def test_heartbeat_republishes_an_unchanged_state() -> None:
    bus = InMemoryBus()
    reg = registration()
    app = make_app(
        bus=bus,
        provider=TestProvider(tick_seconds=None),  # no simulation ticks: nothing changes
        telemetry_heartbeat_seconds=0.05,
        bootstrap_mowers=[reg.model_dump(mode="json")],
    )
    async with lifespan_client(app):
        await wait_until(lambda: len(telemetry(bus, reg.mower_id)) >= 4)
        states = telemetry(bus, reg.mower_id)
        assert {s.activity for s in states} == {states[0].activity}
        assert {s.observed_at for s in states} == {states[0].observed_at}


async def test_heartbeat_carries_the_newest_observed_at_never_a_new_one() -> None:
    bus = InMemoryBus()
    reg = registration()
    app = make_app(
        bus=bus,
        telemetry_heartbeat_seconds=0.05,
        bootstrap_mowers=[reg.model_dump(mode="json")],
    )
    async with lifespan_client(app):
        session = app.state.gateway.session(reg.mower_id)
        await wait_until(lambda: len(telemetry(bus, reg.mower_id)) >= 4)
        newest = session.last_state.observed_at
        states = telemetry(bus, reg.mower_id)
    stamps = [s.observed_at for s in states]
    assert stamps == sorted(stamps)  # never back in time
    assert len(set(stamps)) > 1  # newer, insignificant states reach the heartbeat
    assert stamps[-1] <= newest  # a heartbeat repeats an observed time, it never stamps "now"


async def test_unchanged_ticks_are_not_published_between_heartbeats() -> None:
    bus = InMemoryBus()
    reg = registration()
    app = make_app(
        bus=bus,
        telemetry_heartbeat_seconds=60.0,
        bootstrap_mowers=[reg.model_dump(mode="json")],
    )
    async with lifespan_client(app):
        session = app.state.gateway.session(reg.mower_id)
        await wait_until(lambda: telemetry(bus, reg.mower_id))
        first = session.last_state
        await wait_until(lambda: session.last_state is not first)  # the simulator ticked
        assert len(telemetry(bus, reg.mower_id)) == 1
