# SPDX-FileCopyrightText: 2026 Lukas Körber
# SPDX-License-Identifier: AGPL-3.0-only

"""The message streams hold nothing of a removed mower or an erased account (ADR 0082).

The streams keep a mower's commands, states, snapshots and camera events for up to 30 days.
Fleet owns the mowers and purges their subjects: on ``mower.removed`` and, confirmed, inside the
account erasure. The requests go over the in-memory bus (which keeps the streams' messages like
JetStream does) to the real app.
"""

from uuid import UUID, uuid4

import pytest
from fleet_support import API, Harness, Person, harness
from mowdeck_contracts.v1 import MOWER_REMOVED, ConnectionKind, EventEnvelope, MowerRegistration
from mowdeck_contracts.v1.subjects import (
    camera_relay_event_subject,
    camera_wildcard,
    command_result_subject,
    command_subject,
    device_subject,
    device_wildcard,
    event_subject_for_type,
    telemetry_subject,
)
from mowdeck_service_kit.bus import BusError, BusMessage, DropMessage, InMemoryBus
from mowdeck_service_kit.erasure import ErasureIncomplete
from test_fleet_erasure import erase, rows_of

from fleet.adapters.bus.streams import BusMowerStreams, mower_removed_handler
from fleet.application.demo_purge import AccountPurge
from fleet.application.erasure import STREAM_MESSAGES, ErasureWithStreams


def filters(mower_id: UUID) -> list[str]:
    """Every subject filter that names the mower, one per kind of message."""
    return [
        command_subject(mower_id),
        command_result_subject(mower_id),
        telemetry_subject(mower_id),
        device_wildcard(mower_id),
        camera_wildcard(mower_id),
    ]


async def held(bus: InMemoryBus, mower_id: UUID) -> int:
    """How many messages the streams hold of the mower."""
    return sum([await bus.count_subject(f) for f in filters(mower_id)])


async def traffic(bus: InMemoryBus, mower_id: UUID) -> None:
    """What a mower leaves in the four streams while it runs."""
    await bus.publish(command_subject(mower_id), b'{"issued_by": {"display_name": "Anna"}}')
    await bus.publish(command_result_subject(mower_id), b"{}")
    await bus.publish(telemetry_subject(mower_id), b'{"position": {"lat": 52.5, "lon": 13.4}}')
    await bus.publish(telemetry_subject(mower_id), b'{"position": {"lat": 52.6, "lon": 13.5}}')
    await bus.publish(device_subject(mower_id, "map"), b'{"areas": [{"name": "Front lawn"}]}')
    await bus.publish(device_subject(mower_id, "capabilities"), b"{}")
    await bus.publish(device_subject(mower_id, "path"), b"{}")
    await bus.publish(camera_relay_event_subject(mower_id, "started"), b"{}")


async def mower_of(h: Harness, owner: Person, name: str) -> UUID:
    created = await h.create(owner, "mowers", name=name, connection_kind="simulated")
    return UUID(created["id"])


# --- the account erasure ---------------------------------------------------------------------


async def test_erasure_purges_the_streams_of_the_accounts_mowers_and_no_others() -> None:
    async with harness() as h:
        alice, bob = await h.owner(), await h.owner()
        first, second = await mower_of(h, alice, "A1"), await mower_of(h, alice, "A2")
        other = await mower_of(h, bob, "B1")
        for mower_id in (first, second, other):
            await traffic(h.bus, mower_id)
        messages = await held(h.bus, first) + await held(h.bus, second)
        rows = sum((await rows_of(h.store, alice.account_id)).values())
        bob_messages = await held(h.bus, other)

        reply = await erase(h, alice.account_id)
        again = await erase(h, alice.account_id)

        assert reply.ok and reply.error is None
        # The rows, the "mower.removed" audit rows written on the way, and the messages.
        assert reply.deleted == rows + 2 + messages
        assert await held(h.bus, first) == 0 and await held(h.bus, second) == 0
        assert await held(h.bus, other) == bob_messages
        assert again.ok and again.deleted == 0


async def test_a_consumer_started_after_the_erasure_loads_nothing_of_the_account() -> None:
    """A service that rebuilds its view from the last-value stream on start (Vision's map view,
    Realtime's capabilities) must not get an erased account's map back."""
    async with harness() as h:
        alice, bob = await h.owner(), await h.owner()
        gone, stays = await mower_of(h, alice, "A1"), await mower_of(h, bob, "B1")
        await traffic(h.bus, gone)
        await traffic(h.bus, stays)
        assert (await erase(h, alice.account_id)).ok
        replayed: list[str] = []

        async def view(message: BusMessage) -> None:
            replayed.append(message.subject)

        await h.bus.subscribe("device.mower.>", view, last_per_subject=True)
        await h.bus.subscribe("telemetry.mower.*", view, last_per_subject=True)

        assert replayed and all(str(stays) in subject for subject in replayed)
        assert not any(str(gone) in subject for subject in replayed)


async def test_a_state_the_mower_still_sent_while_it_was_removed_is_purged_too() -> None:
    """The gateway closes a mower's connection on ``mower.removed``; what it published until
    then lands after the first purge. The purge after the rows removes it."""
    async with harness() as h:
        alice = await h.owner()
        mower_id = await mower_of(h, alice, "A1")
        await traffic(h.bus, mower_id)

        async def last_state(message: BusMessage) -> None:
            event = message.model(EventEnvelope[MowerRegistration])
            if event.type == MOWER_REMOVED:
                await h.bus.publish(telemetry_subject(event.payload.mower_id), b"last state")

        await h.bus.subscribe(event_subject_for_type(MOWER_REMOVED), last_state)

        reply = await erase(h, alice.account_id)

        assert reply.ok
        assert await held(h.bus, mower_id) == 0


async def test_a_stream_that_cannot_be_purged_fails_the_erasure_and_keeps_the_rows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Nothing is confirmed while a purge failed, and the rows stay so the retry still knows
    which mowers the account has."""

    async def refuse(self: InMemoryBus, subject: str) -> int:
        del self
        raise BusError(f"purging {subject!r} was refused")

    async with harness() as h:
        alice = await h.owner()
        mower_id = await mower_of(h, alice, "A1")
        await traffic(h.bus, mower_id)
        before = await rows_of(h.store, alice.account_id)
        messages = await held(h.bus, mower_id)
        with monkeypatch.context() as patched:
            patched.setattr(InMemoryBus, "purge_subject", refuse)
            failed = await erase(h, alice.account_id)

        assert failed.ok is False and failed.deleted == 0
        assert failed.error == "BusError: see the log of fleet"
        assert await rows_of(h.store, alice.account_id) == before
        assert await held(h.bus, mower_id) == messages

        retried = await erase(h, alice.account_id)

        assert retried.ok
        assert await rows_of(h.store, alice.account_id) == {}
        assert await held(h.bus, mower_id) == 0


async def test_messages_that_remain_are_reported_like_rows_that_remain() -> None:
    """The check after the purge counts the stream subjects of the mowers it removed."""
    account_id, mower_id = uuid4(), uuid4()

    class Rows:
        async def mower_ids(self, account: UUID) -> list[UUID]:
            assert account == account_id
            return [mower_id]

        async def purge(self, account: UUID) -> int:
            return 3

    class StuckStreams:
        async def purge(self, mower: UUID) -> int:
            return 0

        async def remaining(self, mower: UUID) -> int:
            assert mower == mower_id
            return 2

    async with harness() as h:
        erasure = ErasureWithStreams(Rows(), h.store, StuckStreams())  # type: ignore[arg-type]

        assert await erasure.purge(account_id) == 3
        left = await erasure.leftovers(account_id)

        assert left == {STREAM_MESSAGES: 2}
        assert str(ErasureIncomplete(left)) == "rows of the account remain (stream messages: 2)"
        # The mowers are remembered for one check only.
        assert await erasure.leftovers(account_id) == {}


async def test_the_purge_of_the_demo_sandboxes_is_still_the_row_purge() -> None:
    """``AccountPurge`` is unchanged (ADR 0076); the streams of its mowers go through
    ``mower.removed``."""
    async with harness() as h:
        alice = await h.owner()
        mower_id = await mower_of(h, alice, "A1")
        await traffic(h.bus, mower_id)
        purge: AccountPurge = h.app.state.fleet.purge

        assert await purge.mower_ids(alice.account_id) == [mower_id]
        await purge.purge(alice.account_id)

        assert await held(h.bus, mower_id) == 0
        assert await purge.mower_ids(alice.account_id) == []


# --- a removed mower -------------------------------------------------------------------------


async def test_removing_a_mower_purges_its_subjects_and_keeps_the_other_mowers() -> None:
    async with harness() as h:
        alice = await h.owner()
        gone, stays = await mower_of(h, alice, "A1"), await mower_of(h, alice, "A2")
        await traffic(h.bus, gone)
        await traffic(h.bus, stays)
        kept = await held(h.bus, stays)

        response = await h.client.delete(f"{API}/mowers/{gone}", headers=alice.headers)

        assert response.status_code == 204, response.text
        assert await held(h.bus, gone) == 0
        assert await held(h.bus, stays) == kept


async def test_a_failed_purge_of_a_removed_mower_is_delivered_again() -> None:
    mower_id = uuid4()
    calls: list[UUID] = []

    class Flaky:
        async def purge(self, mower: UUID) -> int:
            calls.append(mower)
            if len(calls) == 1:
                raise BusError("the stream did not answer")
            return 4

        async def remaining(self, mower: UUID) -> int:
            return 0

    bus = InMemoryBus()
    await bus.subscribe(event_subject_for_type(MOWER_REMOVED), mower_removed_handler(Flaky()))
    registration = MowerRegistration(
        mower_id=mower_id, account_id=uuid4(), name="A1", connection_kind=ConnectionKind.SIMULATED
    )
    event = EventEnvelope[MowerRegistration](
        type=MOWER_REMOVED,
        account_id=registration.account_id,
        producer="fleet",
        payload=registration,
    )

    await bus.publish(event_subject_for_type(MOWER_REMOVED), event)

    assert calls == [mower_id, mower_id]  # the first attempt raised, the redelivery purged
    assert bus.dropped == []


async def test_an_event_that_is_not_a_removal_or_not_readable_purges_nothing() -> None:
    class Never:
        async def purge(self, mower: UUID) -> int:
            raise AssertionError("nothing may be purged")

        async def remaining(self, mower: UUID) -> int:
            return 0

    handle = mower_removed_handler(Never())
    with pytest.raises(DropMessage):
        await handle(BusMessage(subject="event.mower.removed", data=b"{not json"))


async def test_without_a_bus_there_is_nothing_to_purge() -> None:
    streams = BusMowerStreams(None)
    assert await streams.purge(uuid4()) == 0
    assert await streams.remaining(uuid4()) == 0
