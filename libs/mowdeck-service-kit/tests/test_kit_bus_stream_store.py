# SPDX-FileCopyrightText: 2026 Lukas Körber
# SPDX-License-Identifier: Apache-2.0

"""``StreamStore`` (ADR 0082): what the streams hold can be removed, in both bus implementations.

The same tests run against :class:`InMemoryBus` and, when ``MOWDECK_TEST_NATS_URL`` points at a
NATS server with JetStream (see ``test_kit_bus_jetstream.py``), against :class:`JetStreamBus`.
Every test uses mower ids and event domains of its own, so the tests do not see each other's
messages in a shared server.
"""

import asyncio
import os
from collections.abc import AsyncIterator
from uuid import uuid4

import pytest
from mowdeck_contracts.v1.subjects import (
    camera_wildcard,
    command_result_subject,
    command_subject,
    device_subject,
    device_wildcard,
    telemetry_subject,
)
from mowdeck_service_kit.bus import (
    BusMessage,
    InMemoryBus,
    JetStreamBus,
    StoredMessage,
    StreamStore,
)

NATS_URL = os.environ.get("MOWDECK_TEST_NATS_URL")

Bus = InMemoryBus | JetStreamBus


@pytest.fixture(params=["memory", "jetstream"])
async def bus(request: pytest.FixtureRequest) -> AsyncIterator[Bus]:
    if request.param == "memory":
        yield InMemoryBus()
        return
    if not NATS_URL:
        pytest.skip("needs MOWDECK_TEST_NATS_URL (a NATS server with JetStream)")
    instance = JetStreamBus(NATS_URL, name="kit-stream-store-tests")
    await instance.connect()
    try:
        yield instance
    finally:
        await instance.close()


def test_both_buses_are_stream_stores() -> None:
    assert isinstance(InMemoryBus(), StreamStore)
    assert isinstance(JetStreamBus("nats://x:4222"), StreamStore)


async def test_purging_a_mower_removes_its_subjects_and_nobody_elses(bus: Bus) -> None:
    gone, stays = uuid4(), uuid4()
    for mower_id in (gone, stays):
        await bus.publish(command_subject(mower_id), b"start")
        await bus.publish(command_result_subject(mower_id), b"done")
        await bus.publish(telemetry_subject(mower_id), b"state 1")
        await bus.publish(telemetry_subject(mower_id), b"state 2")
        await bus.publish(device_subject(mower_id, "map"), b"map")
        await bus.publish(device_subject(mower_id, "capabilities"), b"caps")
        await bus.publish(f"camera.mower.{mower_id}.relay.started", b"camera")
    filters = [
        command_subject(gone),
        command_result_subject(gone),
        telemetry_subject(gone),
        device_wildcard(gone),
        camera_wildcard(gone),
    ]
    assert [await bus.count_subject(f) for f in filters] == [1, 1, 2, 2, 1]

    assert [await bus.purge_subject(f) for f in filters] == [1, 1, 2, 2, 1]

    assert [await bus.count_subject(f) for f in filters] == [0, 0, 0, 0, 0]
    assert await bus.count_subject(telemetry_subject(stays)) == 2
    assert await bus.count_subject(device_wildcard(stays)) == 2
    assert await bus.count_subject(command_subject(stays)) == 1
    # Idempotent: nothing left to purge.
    assert [await bus.purge_subject(f) for f in filters] == [0, 0, 0, 0, 0]


async def test_a_consumer_that_starts_after_the_purge_loads_nothing_of_the_mower(bus: Bus) -> None:
    """The restart of a service that rebuilds its view from a last-value stream (``DEVICE``)."""
    gone, stays = uuid4(), uuid4()
    await bus.publish(device_subject(gone, "map"), b"the erased garden")
    await bus.publish(device_subject(stays, "map"), b"another garden")
    await bus.purge_subject(device_wildcard(gone))
    seen: list[str] = []

    async def handler(message: BusMessage) -> None:
        seen.append(message.subject)

    marker = uuid4()
    subscription = await bus.subscribe("device.mower.*.map", handler, last_per_subject=True)
    await bus.publish(device_subject(marker, "map"), b"live")
    for _ in range(250):
        if device_subject(marker, "map") in seen:
            break
        await asyncio.sleep(0.02)
    await subscription.unsubscribe()
    assert device_subject(stays, "map") in seen
    assert device_subject(gone, "map") not in seen


async def test_single_messages_are_read_and_erased(bus: Bus) -> None:
    domain = "t" + uuid4().hex[:12]
    await bus.publish(f"event.{domain}.first", b"keep")
    await bus.publish(f"event.{domain}.second", b"remove")
    await bus.publish(f"event.{domain}.first", b"remove too")
    stored = [m async for m in bus.stored_messages(f"event.{domain}.>")]
    assert [(m.subject, m.data) for m in stored] == [
        (f"event.{domain}.first", b"keep"),
        (f"event.{domain}.second", b"remove"),
        (f"event.{domain}.first", b"remove too"),
    ]
    assert all(m.stream == "EVENTS" for m in stored)
    assert stored[0].sequence < stored[1].sequence < stored[2].sequence

    erased = [await bus.erase_message(m) for m in stored if m.data.startswith(b"remove")]

    assert erased == [True, True]
    left = [m async for m in bus.stored_messages(f"event.{domain}.>")]
    assert [m.data for m in left] == [b"keep"]
    assert await bus.erase_message(stored[1]) is False  # already gone
    assert await bus.count_subject(f"event.{domain}.>") == 1


async def test_erasing_while_reading_continues_with_the_next_message(bus: Bus) -> None:
    domain = "t" + uuid4().hex[:12]
    for index in range(5):
        await bus.publish(f"event.{domain}.n", str(index).encode())
    seen: list[bytes] = []
    async for message in bus.stored_messages(f"event.{domain}.n"):
        seen.append(message.data)
        await bus.erase_message(message)
    assert seen == [b"0", b"1", b"2", b"3", b"4"]
    assert await bus.count_subject(f"event.{domain}.n") == 0


async def test_headers_come_with_a_stored_message(bus: Bus) -> None:
    domain = "t" + uuid4().hex[:12]
    await bus.publish(f"event.{domain}.h", b"x", msg_id=f"{domain}-1", headers={"K": "v"})
    stored = [m async for m in bus.stored_messages(f"event.{domain}.h")]
    assert stored[0].headers["K"] == "v"
    assert stored[0].headers["Nats-Msg-Id"] == f"{domain}-1"


async def test_core_subjects_are_refused(bus: Bus) -> None:
    with pytest.raises(ValueError, match="no stream stores"):
        await bus.purge_subject("fleet.mowers.list")
    with pytest.raises(ValueError, match="no stream stores"):
        await bus.count_subject("svc.x")
    with pytest.raises(ValueError, match="no stream stores"):
        _ = [m async for m in bus.stored_messages("svc.x")]


async def test_the_memory_bus_keeps_one_message_per_subject_in_a_last_value_stream() -> None:
    bus = InMemoryBus()
    mower_id = uuid4()
    await bus.publish(device_subject(mower_id, "map"), b"old")
    await bus.publish(device_subject(mower_id, "map"), b"new")
    stored: list[StoredMessage] = [m async for m in bus.stored_messages(device_wildcard(mower_id))]
    assert [m.data for m in stored] == [b"new"]
