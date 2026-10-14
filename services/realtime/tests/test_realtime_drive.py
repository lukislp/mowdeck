"""Manual driving over the socket (ADR 0026): start, frames, rate limit, every way it ends.

Fleet is faked on the in-memory bus (:class:`FakeFleet`); the frames are read back from
``command.mower.<id>``. The end to end flow with the real Fleet and Device-Gateway is in
``test_realtime_drive_flow.py``.
"""

import asyncio
from uuid import UUID, uuid4

from mowdeck_contracts.v1 import (
    CommandResult,
    CommandStatus,
    DriveEndReason,
    DriveErrorCode,
    ManualDrive,
    drive_correlation_id,
)
from mowdeck_contracts.v1.realtime import RealtimeCloseCode
from mowdeck_service_kit.bus import publish_command_result
from realtime_support import (
    Client,
    FakeFleet,
    Harness,
    Person,
    driver,
    frames_sent,
    harness,
    publish_capabilities,
    short_token,
)

from realtime.application.drive import TokenBucket


async def _ready(h: Harness, fleet: FakeFleet | None = None) -> tuple[Person, UUID]:
    await (fleet or FakeFleet()).attach(h.bus)
    owner = await h.owner()
    mower_id = await h.add_mower(owner)
    await publish_capabilities(h.bus, mower_id, owner.account_id)
    return owner, mower_id


async def _start(client: Client, mower_id: UUID, **extra: object) -> dict[str, object]:
    await client.send(
        {"type": "drive_start", "request_id": "d", "mower_id": str(mower_id), **extra}
    )
    return await client.expect("drive_started", request_id="d")


async def _frame(
    client: Client, session_id: object, seq: int | None = None, **extra: object
) -> None:
    message: dict[str, object] = {
        "type": "drive_frame",
        "session_id": session_id,
        "linear": 0.5,
        "angular": -0.25,
        **extra,
    }
    if seq is not None:
        message["seq"] = seq
    await client.send(message)


async def test_realtime_drive_start_frames_and_stop() -> None:
    async with harness() as h:
        fleet = FakeFleet()
        owner, mower_id = await _ready(h, fleet)
        client = await h.connect_as(owner)
        started = await _start(client, mower_id, blade_on_allowed=True)
        assert started["max_rate_hz"] == 10
        assert started["ttl_ms"] == 1000
        assert started["max_seconds"] == 600
        assert started["blade_on_allowed"] is True
        assert started["capabilities"] == {"manual_drive": True, "manual_drive_blades": True}
        [request] = fleet.starts
        assert request.on_behalf_of.subject == owner.principal.subject
        assert request.account_id == owner.account_id
        assert request.blade_on_allowed is True
        assert request.max_seconds == 600
        session_id = started["session_id"]
        assert h.app.state.hub.drive_count == 1
        await _frame(client, session_id, seq=1, blade_on=True)
        ack = await client.expect("drive_frame_ack")
        assert ack == {"type": "drive_frame_ack", "session_id": session_id, "seq": 1}
        await _frame(client, session_id)  # without seq: no ack
        await client.nothing(0.2)
        [first, second] = frames_sent(h.bus, mower_id)
        assert isinstance(first.command, ManualDrive)
        assert (first.command.linear, first.command.angular) == (0.5, -0.25)
        assert first.command.blade_on is True
        assert first.command.ttl_ms == 1000
        assert first.correlation_id == drive_correlation_id(UUID(str(session_id)))
        assert first.account_id == owner.account_id
        assert first.issued_by.subject == owner.principal.subject
        assert (first.expires_at - first.issued_at).total_seconds() == 1.0
        assert second.command.kind == "manual_drive"
        await client.send({"type": "drive_stop", "request_id": "x", "session_id": session_id})
        ended = await client.expect("drive_ended")
        assert ended["reason"] == "client_stop"
        stop = frames_sent(h.bus, mower_id)[-1]
        assert isinstance(stop.command, ManualDrive)
        assert (stop.command.linear, stop.command.angular, stop.command.blade_on) == (0, 0, False)
        await asyncio.sleep(0.05)
        [closed] = fleet.stops
        assert closed.reason is DriveEndReason.CLIENT_STOP
        assert (closed.frames, closed.frames_dropped) == (2, 0)
        assert h.app.state.hub.drive_count == 0
        # Frames after the end are refused.
        await _frame(client, session_id)
        error = await client.expect("error")
        assert error["code"] == "drive_not_active"
        await client.send({"type": "drive_stop", "request_id": "y", "session_id": session_id})
        assert (await client.expect("error", request_id="y"))["code"] == "drive_not_active"


async def test_realtime_drive_requires_can_drive() -> None:
    async with harness() as h:
        fleet = FakeFleet()
        owner, mower_id = await _ready(h, fleet)
        viewer = await h.viewer(owner)
        stranger = await h.owner()
        for person in (viewer, stranger):
            client = await h.connect_as(person)
            await client.send({"type": "drive_start", "request_id": "d", "mower_id": str(mower_id)})
            error = await client.expect("error", request_id="d")
            assert error["code"] == "drive_not_allowed"
        assert fleet.starts == []  # refused before Fleet is asked
        allowed = await driver(h, owner)
        client = await h.connect_as(allowed)
        started = await _start(client, mower_id, blade_on_allowed=True)
        assert started["session_id"]


async def test_realtime_drive_unsupported_mowers_and_blades() -> None:
    async with harness() as h:
        fleet = FakeFleet()
        owner, _ = await _ready(h, fleet)
        cloud = await h.add_mower(owner)
        await publish_capabilities(h.bus, cloud, owner.account_id, manual_drive=False)
        unknown = await h.add_mower(owner)  # no capabilities known
        foreign_caps = await h.add_mower(owner)
        await publish_capabilities(h.bus, foreign_caps, uuid4())  # names another account
        client = await h.connect_as(owner)
        for target in (cloud, unknown, foreign_caps):
            await client.send({"type": "drive_start", "request_id": "u", "mower_id": str(target)})
            error = await client.expect("error", request_id="u")
            assert error["code"] == "drive_unsupported"
        # Blades: only when the mower allows them.
        no_blades = await h.add_mower(owner)
        await publish_capabilities(h.bus, no_blades, owner.account_id, manual_drive_blades=False)
        started = await _start(client, no_blades, blade_on_allowed=True)
        assert started["blade_on_allowed"] is False
        await _frame(client, started["session_id"], blade_on=True)
        await asyncio.sleep(0.05)
        [frame] = frames_sent(h.bus, no_blades)
        assert isinstance(frame.command, ManualDrive) and frame.command.blade_on is False
        assert fleet.starts[-1].blade_on_allowed is False


async def test_realtime_drive_busy_and_fleet_unavailable() -> None:
    async with harness() as h:
        fleet = FakeFleet()
        owner, mower_id = await _ready(h, fleet)
        client = await h.connect_as(owner)
        fleet.error = DriveErrorCode.BUSY
        await client.send({"type": "drive_start", "request_id": "b", "mower_id": str(mower_id)})
        assert (await client.expect("error", request_id="b"))["code"] == "drive_busy"
        fleet.error = DriveErrorCode.FORBIDDEN
        await client.send({"type": "drive_start", "request_id": "f", "mower_id": str(mower_id)})
        assert (await client.expect("error", request_id="f"))["code"] == "drive_not_allowed"
    async with harness(realtime_drive_request_timeout_seconds=0.2) as h:
        owner = await h.owner()
        mower_id = await h.add_mower(owner)
        await publish_capabilities(h.bus, mower_id, owner.account_id)
        client = await h.connect_as(owner)  # nobody answers fleet.drive.start
        await client.send({"type": "drive_start", "request_id": "n", "mower_id": str(mower_id)})
        assert (await client.expect("error", request_id="n"))["code"] == "drive_unavailable"
        h.app.state.hub.drive = None  # manual driving disabled
        await client.send({"type": "drive_start", "request_id": "o", "mower_id": str(mower_id)})
        assert (await client.expect("error", request_id="o"))["code"] == "drive_unavailable"


async def test_realtime_drive_frames_are_rate_limited() -> None:
    async with harness(realtime_drive_max_rate_hz=5) as h:
        fleet = FakeFleet()
        owner, mower_id = await _ready(h, fleet)
        client = await h.connect_as(owner)
        started = await _start(client, mower_id)
        assert started["max_rate_hz"] == 5
        for seq in range(20):  # a burst far above 5 per second
            await _frame(client, started["session_id"], seq=seq)
        await client.expect("drive_frame_ack", seq=0)
        await asyncio.sleep(0.1)
        published = len(frames_sent(h.bus, mower_id))
        assert 1 <= published <= 3
        await client.send({"type": "drive_stop", "session_id": started["session_id"]})
        await client.expect("drive_ended", reason="client_stop")
        await asyncio.sleep(0.05)
        [stop] = fleet.stops
        assert stop.frames == published
        assert stop.frames_dropped == 20 - published


def test_realtime_drive_token_bucket_refills() -> None:
    bucket = TokenBucket(10, 2, now=0.0)
    assert [bucket.allow(0.0) for _ in range(3)] == [True, True, False]
    assert bucket.allow(0.1)  # one token after a tenth of a second
    assert not bucket.allow(0.1)
    assert bucket.allow(10.0) and bucket.allow(10.0) and not bucket.allow(10.0)


async def test_realtime_drive_socket_close_stops_the_mower() -> None:
    async with harness() as h:
        fleet = FakeFleet()
        owner, mower_id = await _ready(h, fleet)
        client = await h.connect_as(owner)
        started = await _start(client, mower_id)
        await _frame(client, started["session_id"], seq=1)
        await client.expect("drive_frame_ack")
        await client.ws.close()
        async with asyncio.timeout(3):
            while not fleet.stops:  # noqa: ASYNC110 - polling the fake
                await asyncio.sleep(0.01)
        [stop] = fleet.stops
        assert stop.reason is DriveEndReason.SOCKET_CLOSED
        assert stop.frames == 1
        last = frames_sent(h.bus, mower_id)[-1]
        assert isinstance(last.command, ManualDrive) and last.command.linear == 0
        assert h.app.state.hub.drive_count == 0


async def test_realtime_drive_ends_after_the_maximum_duration() -> None:
    async with harness(realtime_drive_max_seconds=1) as h:
        fleet = FakeFleet()
        owner, mower_id = await _ready(h, fleet)
        client = await h.connect_as(owner)
        started = await _start(client, mower_id)
        assert started["max_seconds"] == 1
        ended = await client.expect("drive_ended", timeout=3)
        assert ended["reason"] == "max_duration"
        await asyncio.sleep(0.05)
        assert fleet.stops[-1].reason is DriveEndReason.MAX_DURATION


async def test_realtime_drive_refused_frame_ends_the_session() -> None:
    async with harness() as h:
        owner, mower_id = await _ready(h)
        watcher = await h.connect_as(owner)
        await watcher.subscribe(mower_id)
        client = await h.connect_as(owner)
        started = await _start(client, mower_id)
        session_id = UUID(str(started["session_id"]))
        correlation = drive_correlation_id(session_id)
        accepted = CommandResult(
            command_id=uuid4(),
            mower_id=mower_id,
            status=CommandStatus.ACCEPTED,
            correlation_id=correlation,
        )
        await publish_command_result(h.bus, accepted)
        await watcher.nothing(0.2)  # accepted frame results are not fanned out
        busy = CommandResult(
            command_id=uuid4(),
            mower_id=mower_id,
            status=CommandStatus.REJECTED,
            reason="pause or end the job before driving manually",
            correlation_id=correlation,
        )
        await publish_command_result(h.bus, busy)
        ended = await client.expect("drive_ended")
        assert ended["reason"] == "mower_busy"
        assert "pause" in ended["detail"]
        assert (await watcher.expect("command_result"))["result"]["status"] == "rejected"
        second = await _start(client, mower_id)
        refused = CommandResult(
            command_id=uuid4(),
            mower_id=mower_id,
            status=CommandStatus.FAILED,
            reason="mower is not connected",
            correlation_id=drive_correlation_id(UUID(str(second["session_id"]))),
        )
        await publish_command_result(h.bus, refused)
        assert (await client.expect("drive_ended"))["reason"] == "mower_refused"
        # A late refusal of an ended session changes nothing.
        await publish_command_result(h.bus, busy.model_copy(update={"command_id": uuid4()}))
        await client.nothing(0.2)


async def test_realtime_drive_revoked_when_can_drive_is_taken_away() -> None:
    async with harness() as h:
        fleet = FakeFleet()
        owner, mower_id = await _ready(h, fleet)
        person = await driver(h, owner)
        client = await h.connect_as(person)
        await _start(client, mower_id)
        await h.revoke_drive(person)
        ended = await client.expect("drive_ended")
        assert ended["reason"] == "revoked"
        await asyncio.sleep(0.05)
        assert fleet.stops[-1].reason is DriveEndReason.REVOKED


async def test_realtime_drive_ends_with_the_token() -> None:
    async with harness() as h:
        fleet = FakeFleet()
        owner, mower_id = await _ready(h, fleet)
        client = await h.connect()
        await client.auth(short_token(h.keys, owner.principal, 2))
        await _start(client, mower_id)
        ended = await client.expect("drive_ended", timeout=4)
        assert ended["reason"] == "token_expired"
        assert await client.closed_code() == RealtimeCloseCode.UNAUTHORIZED


async def test_realtime_drive_new_start_replaces_the_session_and_bad_frames_are_invalid() -> None:
    async with harness() as h:
        fleet = FakeFleet()
        owner, mower_id = await _ready(h, fleet)
        client = await h.connect_as(owner)
        first = await _start(client, mower_id)
        await client.send({"type": "drive_start", "request_id": "e", "mower_id": str(mower_id)})
        ended = await client.expect("drive_ended")
        assert ended["reason"] == "replaced"
        assert ended["session_id"] == first["session_id"]
        second = await client.expect("drive_started", request_id="e")
        assert second["session_id"] != first["session_id"]
        assert frames_sent(h.bus, mower_id) == []  # no frame was live: no stop frame either
        await _frame(client, second["session_id"], linear=2.0)  # outside -1..1
        assert (await client.expect("error"))["code"] == "invalid_message"
        await _frame(client, str(uuid4()))
        assert (await client.expect("error"))["code"] == "drive_not_active"
