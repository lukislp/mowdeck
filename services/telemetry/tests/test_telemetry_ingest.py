"""Consumers over the in-memory bus and the memory store: states, results, directory, seed."""

import asyncio
from uuid import uuid4

from mowdeck_contracts.v1 import (
    CommandResult,
    CommandStatus,
    ConnectionKind,
    EventEnvelope,
    MowerActivity,
    MowerListReply,
    MowerListRequest,
    MowerRegistration,
)
from mowdeck_contracts.v1.subjects import (
    FLEET_MOWERS_LIST,
    command_result_subject,
    telemetry_subject,
)
from mowdeck_service_kit.bus import BusMessage, publish_event
from telemetry_support import T0, harness, make_state, seconds

from telemetry.adapters.bus.fleet import backoff, seed_directory
from telemetry.adapters.bus.subscriptions import (
    MOWERS_CONSUMER,
    RESULTS_CONSUMER,
    STATES_CONSUMER,
)
from telemetry.application.ports import RetentionTarget


async def test_telemetry_state_upserts_latest_and_ignores_older_observed_at() -> None:
    async with harness() as h:
        owner = await h.owner()
        mower_id = await h.add_mower(owner)
        await h.publish_state(make_state(mower_id, owner.account_id, at=T0, battery=90))
        await h.publish_state(
            make_state(mower_id, owner.account_id, at=T0 + seconds(30), battery=88)
        )
        await h.publish_state(  # arrives late: older than the stored one
            make_state(mower_id, owner.account_id, at=T0 + seconds(10), battery=50)
        )
        latest = await h.store.latest_of(owner.account_id, mower_id)
        history = h.store.data.history
    assert latest is not None
    assert (latest.observed_at, latest.battery_percent) == (T0 + seconds(30), 88)
    assert sorted(r.battery_percent for r in history.values()) == [88, 90]


async def test_telemetry_redelivered_state_changes_nothing() -> None:
    async with harness() as h:
        owner = await h.owner()
        mower_id = await h.add_mower(owner)
        state = make_state(mower_id, owner.account_id, position=(48.1, 11.5))
        await h.publish_state(state)
        await h.publish_state(state)
        assert len(h.store.data.history) == 1
        assert len(h.store.data.positions) == 1


async def test_telemetry_history_skips_unchanged_heartbeats_but_keeps_the_periodic_row() -> None:
    async with harness(telemetry_heartbeat_history_seconds=60) as h:
        owner = await h.owner()
        mower_id = await h.add_mower(owner)
        for step in range(0, 131, 10):  # a heartbeat every 10 s for 130 s
            await h.publish_state(make_state(mower_id, owner.account_id, at=T0 + seconds(step)))
        await h.publish_state(
            make_state(mower_id, owner.account_id, at=T0 + seconds(140), battery=89)
        )
        stored = sorted(r.observed_at for r in h.store.data.history.values())
    assert stored == [T0, T0 + seconds(60), T0 + seconds(120), T0 + seconds(140)]


async def test_telemetry_positions_are_appended_only_when_they_differ() -> None:
    async with harness() as h:
        owner = await h.owner()
        mower_id = await h.add_mower(owner)
        track = [(48.1, 11.5), (48.1, 11.5), (48.1001, 11.5), (48.1001, 11.5002)]
        for i, position in enumerate(track):
            await h.publish_state(
                make_state(mower_id, owner.account_id, at=T0 + seconds(i), position=position)
            )
        await h.publish_state(make_state(mower_id, owner.account_id, at=T0 + seconds(9)))
        stored = [
            (p.lat, p.lon)
            for p in sorted(h.store.data.positions.values(), key=lambda p: p.observed_at)
        ]
    assert stored == [(48.1, 11.5), (48.1001, 11.5), (48.1001, 11.5002)]


async def test_telemetry_state_with_a_foreign_account_is_dropped() -> None:
    async with harness() as h:
        owner = await h.owner()
        other = await h.owner()
        mower_id = await h.add_mower(owner)
        await h.publish_state(make_state(mower_id, owner.account_id))
        await h.publish_state(make_state(mower_id, other.account_id, at=T0 + seconds(5)))
        stranger = await h.add_mower(other, announce=False)
        await h.publish_state(
            make_state(stranger, other.account_id)
        )  # unknown to the directory: ok
        assert len(h.bus.dropped) == 1
        latest = await h.store.latest_of(owner.account_id, mower_id)
        assert await h.store.latest_of(other.account_id, mower_id) is None
        assert await h.store.latest_of(other.account_id, stranger) is not None
    assert latest is not None and latest.observed_at == T0


async def test_telemetry_state_whose_stored_account_differs_is_dropped() -> None:
    async with harness() as h:
        owner = await h.owner()
        other = await h.owner()
        mower_id = uuid4()  # not in the directory: the stored row is the reference
        await h.publish_state(make_state(mower_id, owner.account_id))
        await h.publish_state(make_state(mower_id, other.account_id, at=T0 + seconds(5)))
        assert len(h.bus.dropped) == 1


async def test_telemetry_malformed_and_mismatched_messages_are_dropped_not_retried() -> None:
    async with harness() as h:
        owner = await h.owner()
        mower_id = await h.add_mower(owner)
        other_id = uuid4()
        await h.bus.publish(telemetry_subject(mower_id), b"not json")
        await h.bus.publish(telemetry_subject(mower_id), b'{"mower_id": "x"}')
        # the subject names another mower than the payload
        await h.bus.publish(telemetry_subject(other_id), make_state(mower_id, owner.account_id))
        await h.bus.publish(command_result_subject(mower_id), b"not json")
        await h.bus.publish(
            command_result_subject(other_id),
            CommandResult(command_id=uuid4(), mower_id=mower_id, status=CommandStatus.EXECUTED),
        )
        await h.bus.publish("event.mower.registered", b"{}")
        await h.bus.publish(
            "event.mower.registered",
            EventEnvelope[dict[str, str]](
                type="mower.registered", producer="fleet", payload={"nope": "x"}
            ),
        )
        assert len(h.bus.dropped) == 7
        assert h.store.data.latest == {} and h.store.data.commands == {}


async def test_telemetry_results_resolve_the_account_from_the_directory() -> None:
    async with harness() as h:
        owner = await h.owner()
        mower_id = await h.add_mower(owner)
        command_id = await h.publish_result(mower_id, CommandStatus.ACCEPTED, at=T0)
        await h.publish_result(
            mower_id, CommandStatus.EXECUTED, command_id=command_id, at=T0 + seconds(2)
        )
        await h.publish_result(
            mower_id, CommandStatus.EXECUTED, command_id=command_id, at=T0 + seconds(2)
        )  # redelivery
        events = await h.store.commands(owner.account_id, mower_id, 10)
        none = await h.store.commands(uuid4(), mower_id, 10)
    assert [e.status for e in events] == ["executed", "accepted"]  # newest first
    assert {e.account_id for e in events} == {owner.account_id}
    assert none == []


async def test_telemetry_result_for_an_unknown_mower_is_retried_then_dropped() -> None:
    async with harness(telemetry_unknown_mower_attempts=3) as h:
        owner = await h.owner()
        ingest = h.app.state.telemetry.ingest
        original = ingest.record_result
        deliveries: list[object] = []

        async def counting(result: CommandResult) -> None:
            deliveries.append(result)
            await original(result)

        ingest.record_result = counting
        await h.publish_result(uuid4(), CommandStatus.EXECUTED)
        assert len(deliveries) == 3  # nak, nak, then dropped with a warning
        assert len(h.bus.dropped) == 1 and h.store.data.commands == {}
        # a result for a mower the directory knows is stored on the first delivery
        known = await h.add_mower(owner)
        await h.publish_result(known, CommandStatus.EXECUTED)
        assert len(deliveries) == 4 and len(h.store.data.commands) == 1


async def test_telemetry_unknown_mower_result_is_stored_when_the_event_arrives_meanwhile() -> None:
    async with harness(telemetry_unknown_mower_attempts=5) as h:
        owner = await h.owner()
        mower_id = await h.add_mower(owner, announce=False)
        ingest = h.app.state.telemetry.ingest
        original = ingest.record_result
        calls = 0

        async def late_event(result: CommandResult) -> None:
            nonlocal calls
            calls += 1
            if calls == 2:
                await h.announce(mower_id, owner.account_id)
            await original(result)

        ingest.record_result = late_event
        await h.publish_result(mower_id, CommandStatus.EXECUTED)
        assert calls == 2 and h.bus.dropped == []
        assert len(h.store.data.commands) == 1


async def test_telemetry_directory_follows_fleet_events() -> None:
    async with harness() as h:
        owner = await h.owner()
        mower_id = await h.add_mower(owner)
        await h.publish_state(make_state(mower_id, owner.account_id, position=(1.0, 2.0)))
        await h.publish_result(mower_id, CommandStatus.EXECUTED)
        assert h.store.data.directory[mower_id].account_id == owner.account_id
        # connection events share the domain and are ignored
        await h.bus.publish("event.mower.connected", b'{"type": "mower.connected"}')
        # removal purges the mower, and only that mower
        other = await h.add_mower(owner)
        await h.publish_state(make_state(other, owner.account_id))
        await h.announce(mower_id, owner.account_id, removed=True)
        data = h.store.data
        assert mower_id not in data.directory and mower_id not in data.latest
        assert not [k for k in data.history if k[0] == mower_id]
        assert not [k for k in data.positions if k[0] == mower_id]
        assert not [c for c in data.commands.values() if c.mower_id == mower_id]
        assert other in data.directory and other in data.latest


async def test_telemetry_registration_update_changes_the_directory_entry() -> None:
    async with harness() as h:
        owner = await h.owner()
        mower_id = uuid4()
        await h.announce(mower_id, owner.account_id)
        await publish_event(
            h.bus,
            EventEnvelope[MowerRegistration](
                type="mower.updated",
                producer="fleet",
                payload=MowerRegistration(
                    mower_id=mower_id,
                    account_id=owner.account_id,
                    name="Renamed",
                    connection_kind=ConnectionKind.SIMULATED,
                ),
            ),
        )
        assert h.store.data.directory[mower_id].account_id == owner.account_id


async def test_telemetry_seed_loads_the_directory_from_fleet() -> None:
    async with harness() as h:
        owner = await h.owner()
        mine = uuid4()
        known = uuid4()
        other_account = uuid4()
        await h.announce(known, owner.account_id)  # the event already told us

        async def fleet(message: BusMessage) -> None:
            assert message.model(MowerListRequest) == MowerListRequest(shard_index=0, shard_count=1)
            await message.respond(
                MowerListReply(
                    mowers=[
                        MowerRegistration(
                            mower_id=mine,
                            account_id=owner.account_id,
                            name="a",
                            connection_kind=ConnectionKind.SIMULATED,
                        ),
                        MowerRegistration(  # the event wins over the seed
                            mower_id=known,
                            account_id=other_account,
                            name="b",
                            connection_kind=ConnectionKind.SIMULATED,
                        ),
                    ]
                )
            )

        await h.bus.subscribe(FLEET_MOWERS_LIST, fleet, queue="fleet")
        seeded: list[int] = []
        count = await seed_directory(
            h.bus,
            h.app.state.telemetry.ingest,
            timeout_seconds=1,
            retry_max_seconds=1,
            on_seeded=seeded.append,
        )
        directory = h.store.data.directory
    assert count == 2 and seeded == [2]
    assert directory[mine].account_id == owner.account_id
    assert directory[known].account_id == owner.account_id


async def test_telemetry_seed_retries_while_fleet_is_absent() -> None:
    async with harness() as h:
        owner = await h.owner()
        mower_id = uuid4()
        task = asyncio.create_task(
            seed_directory(
                h.bus,
                h.app.state.telemetry.ingest,
                timeout_seconds=1,
                retry_max_seconds=0.05,
                first_delay=0.01,
            )
        )
        await asyncio.sleep(0.05)  # no responder yet: the request fails and is retried
        assert not task.done()

        async def fleet(message: BusMessage) -> None:
            await message.respond(
                MowerListReply(
                    mowers=[
                        MowerRegistration(
                            mower_id=mower_id,
                            account_id=owner.account_id,
                            name="late",
                            connection_kind=ConnectionKind.SIMULATED,
                        )
                    ]
                )
            )

        await h.bus.subscribe(FLEET_MOWERS_LIST, fleet)
        assert await asyncio.wait_for(task, 2) == 1
        assert mower_id in h.store.data.directory


async def test_telemetry_app_seeds_in_the_background_and_reports_it() -> None:
    async with harness(background_tasks=True) as h:
        owner = await h.owner()
        mower_id = uuid4()

        async def fleet(message: BusMessage) -> None:
            await message.respond(
                MowerListReply(
                    mowers=[
                        MowerRegistration(
                            mower_id=mower_id,
                            account_id=owner.account_id,
                            name="x",
                            connection_kind=ConnectionKind.SIMULATED,
                        )
                    ]
                )
            )

        await h.bus.subscribe(FLEET_MOWERS_LIST, fleet)
        for _ in range(100):
            if h.app.state.directory_seeded:
                break
            await asyncio.sleep(0.05)
        assert h.app.state.directory_seeded is True
        assert mower_id in h.store.data.directory


def test_telemetry_backoff_doubles_up_to_the_maximum() -> None:
    delays = backoff(1.0, 5.0)
    assert [next(delays) for _ in range(5)] == [1.0, 2.0, 4.0, 5.0, 5.0]


async def test_telemetry_consumer_names_are_the_documented_queue_groups() -> None:
    assert (STATES_CONSUMER, RESULTS_CONSUMER, MOWERS_CONSUMER) == (
        "telemetry-states",
        "telemetry-results",
        "telemetry-mowers",
    )
    async with harness() as h:
        assert len(h.app.state.subscriptions) == 5  # four consumers and the track responder
        assert h.app.state.telemetry.ingest is not None
        assert MowerActivity.DOCKED.value == "docked"
        assert RetentionTarget.HISTORY.value == "mower_history"
