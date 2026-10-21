"""The agora source driver against a fake bridge server: join, frames to muxers, renewal and
rejoin, crash, stop, availability and secrets (ADR 0045 step 7)."""

import asyncio
import json
import os
import struct
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any
from uuid import uuid4

import pytest
import structlog
from bridge_support import (
    APP_ID,
    CHANNEL,
    DELTA,
    KEY,
    KEYFRAME,
    SALT,
    SECRETS,
    TOKEN,
    BridgeConnection,
    FakeBridgeServer,
    FakeSpawns,
    agora_session,
    eventually,
    frame,
    fresh_token,
)
from mowdeck_contracts.v1 import (
    CameraReleaseReply,
    CameraReleaseRequest,
    CameraSession,
    CameraSessionReply,
)
from mowdeck_contracts.v1.camera import CameraProvider
from mowdeck_contracts.v1.subjects import ALL_CAMERA_RELEASES, ALL_CAMERA_SESSIONS
from mowdeck_service_kit.bus import BusMessage, InMemoryBus
from relay_support import T0, FakeClock, key

from camera_relay.adapters.gateway import GatewayVendorSessions, service_principal
from camera_relay.adapters.media.bridge_protocol import (
    BridgeFrame,
    BridgeProtocolError,
    RecordReader,
    decode_record,
    encode_event,
    encode_frame,
    join_line,
)
from camera_relay.adapters.media.bridge_source import (
    BridgeEndpoint,
    BridgeSourceDriver,
    muxer_args,
)
from camera_relay.adapters.media.mpegts import AUD, CLOCK, TsWriter, crc32_mpeg2
from camera_relay.adapters.media.pipeline import MediaServer, RunningSource, source_path
from camera_relay.application.ports import UnsupportedProviderError
from camera_relay.domain.models import RelayKey

RID = "fedcba9876543210fedcba9876543210"
SERVER = MediaServer("rtsp://mediamtx:8554", "relay", lambda: "publisher-pw")


# --- MPEG-TS -----------------------------------------------------------------------------------


def _packets(stream: bytes) -> list[bytes]:
    assert len(stream) % 188 == 0
    packets = [stream[i : i + 188] for i in range(0, len(stream), 188)]
    assert all(p[0] == 0x47 for p in packets)
    return packets


@dataclass
class Pes:
    pts: int
    payload: bytes
    random_access: bool


def _pes(stream: bytes, pid: int = 0x100) -> list[Pes]:
    """The PES packets of ``pid`` with their PTS and payload (without the PES header)."""
    out: list[Pes] = []
    current: bytearray | None = None
    random_access = False
    for packet in _packets(stream):
        if ((packet[1] & 0x1F) << 8 | packet[2]) != pid:
            continue
        start = bool(packet[1] & 0x40)
        control = (packet[3] >> 4) & 0x3
        offset = 4
        flags = 0
        if control & 0x2:
            length = packet[4]
            flags = packet[5] if length else 0
            offset += 1 + length
        if start:
            if current is not None:
                out.append(_finish(current, random_access))
            current = bytearray()
            random_access = bool(flags & 0x40)
        if current is not None and control & 0x1:
            current += packet[offset:]
    if current is not None:
        out.append(_finish(current, random_access))
    return out


def _finish(data: bytearray, random_access: bool) -> Pes:
    assert data[:4] == b"\x00\x00\x01\xe0"
    header_length = data[8]
    p = data[9:14]
    pts = ((p[0] >> 1) & 0x07) << 30 | p[1] << 22 | (p[2] >> 1) << 15 | p[3] << 7 | (p[4] >> 1)
    return Pes(pts=pts, payload=bytes(data[9 + header_length :]), random_access=random_access)


def test_the_writer_starts_with_psi_and_a_lone_delimiter_one_frame_early() -> None:
    writer = TsWriter(2)
    stream = writer.start(CLOCK) + writer.frame(KEYFRAME, CLOCK, keyframe=True)
    stream += writer.frame(DELTA, CLOCK + 9000, keyframe=False)
    pes = _pes(stream)
    assert [p.pts for p in pes] == [CLOCK - 9000, CLOCK, CLOCK + 9000]
    assert pes[0].payload == AUD[2]  # starts the first access unit
    assert pes[1].payload == KEYFRAME + AUD[2] and pes[1].random_access
    assert pes[2].payload == DELTA + AUD[2] and not pes[2].random_access


def _section(packet: bytes) -> bytes:
    offset = 4 + (1 + packet[4] if packet[3] & 0x20 else 0)
    start = offset + 1 + packet[offset]  # pointer field
    length = (packet[start + 1] & 0x0F) << 8 | packet[start + 2]
    return packet[start : start + 3 + length]


def test_psi_sections_carry_valid_crcs_and_the_hevc_stream_type() -> None:
    pat, pmt = (_section(p) for p in _packets(TsWriter(2).psi()))
    assert pat[0] == 0x00 and pmt[0] == 0x02
    assert crc32_mpeg2(pat) == 0  # a section including its CRC checks to zero
    assert crc32_mpeg2(pmt) == 0
    assert pmt[12] == 0x24  # HEVC
    assert _section(_packets(TsWriter(1).psi())[1])[12] == 0x1B  # H.264


def test_large_access_units_span_packets_and_keep_their_bytes() -> None:
    unit = b"\x00\x00\x00\x01\x26\x01" + bytes(range(256)) * 400  # > 65535: unbounded PES
    pes = _pes(TsWriter(2).frame(unit, 12345, keyframe=True))
    assert pes[0].payload == unit + AUD[2] and pes[0].pts == 12345


def test_the_writer_refuses_other_codecs() -> None:
    with pytest.raises(ValueError, match="codec"):
        TsWriter(9)


# --- Protocol -----------------------------------------------------------------------------------


def test_records_follow_the_documented_layout() -> None:
    record = encode_frame(BridgeFrame(2, 2, True, 5, 7, b"\0\0\1x"))
    body = b"\x01" + struct.pack(">IBBQQ", 2, 2, 3, 5, 7) + b"\0\0\1x"
    assert record == struct.pack(">I", len(body)) + body
    assert decode_record(record[4:]) == BridgeFrame(2, 2, True, 5, 7, b"\0\0\1x")
    assert RecordReader().feed(encode_event("joined", elapsed_ms=3)) == [
        {"event": "joined", "elapsed_ms": 3}
    ]
    with pytest.raises(BridgeProtocolError, match="out of range"):
        RecordReader().feed(struct.pack(">I", 4 * 1024 * 1024 + 65) + b"\x02")


def test_the_join_line_carries_the_session_and_nothing_else() -> None:
    line = json.loads(join_line(agora_session()))
    assert line == {
        "op": "join",
        "app_id": APP_ID,
        "channel": CHANNEL,
        "token": TOKEN,
        "uid": 900001,
        "encryption": {"mode": "aes-256-gcm2", "key": KEY, "salt": SALT},
    }
    assert json.loads(join_line(agora_session(encrypted=False)))["encryption"] is None


# --- The driver ---------------------------------------------------------------------------------


@dataclass
class Harness:
    bridge: FakeBridgeServer
    spawns: FakeSpawns
    driver: BridgeSourceDriver
    clock: FakeClock
    failures: list[RunningSource] = field(default_factory=list)
    fresh: list[CameraSession] = field(default_factory=list)
    fetched: list[RelayKey] = field(default_factory=list)
    sleeps: list[float] = field(default_factory=list)
    wake: asyncio.Queue[None] = field(default_factory=asyncio.Queue)

    async def fetch(self, relay_key: RelayKey) -> CameraSession:
        self.fetched.append(relay_key)
        return self.fresh.pop(0)

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        await self.wake.get()


@pytest.fixture
async def harness() -> AsyncIterator[Harness]:
    bridge = FakeBridgeServer()
    port = await bridge.start()
    spawns = FakeSpawns()
    clock = FakeClock(T0)
    holder: list[Harness] = []
    driver = BridgeSourceDriver(
        endpoint=BridgeEndpoint("127.0.0.1", port),
        server=SERVER,
        ffmpeg="ffmpeg",
        on_failure=lambda source: holder[0].failures.append(source),
        fetch_session=lambda k: holder[0].fetch(k),
        spawn=spawns.plain,
        spawn_stdin=spawns.stdin,
        now=clock,
        sleep=lambda s: holder[0].sleep(s),
        transcoder_delay=0,
        switch_timeout=5,
    )
    h = Harness(bridge, spawns, driver, clock)
    holder.append(h)
    try:
        yield h
    finally:
        for source in [run.source for run in list(driver.runs.values())]:
            await driver.stop(source)
        await bridge.close()


def _source(
    session: CameraSession, cameras: tuple[str, ...] = ("front_left", "front_right")
) -> RunningSource:
    return RunningSource(
        key=key(),
        session_id=uuid4(),
        relay_id=RID,
        provider=CameraProvider.AGORA,
        cameras=cameras,
        channel=session.channel,
        session=session,
    )


async def _started(
    h: Harness, session: CameraSession | None = None
) -> tuple[RunningSource, BridgeConnection]:
    source = _source(session or agora_session())
    await h.driver.start(source)
    connection = await h.bridge.connection(0)
    await eventually(lambda: connection.ops() == ["join", "subscribe"])
    return source, connection


async def _settle() -> None:
    for _ in range(10):
        await asyncio.sleep(0)


async def test_start_joins_with_the_session_and_subscribes_the_leased_cameras(
    harness: Harness,
) -> None:
    _, connection = await _started(harness)
    join, subscribe = connection.lines
    assert join["token"] == TOKEN and join["channel"] == CHANNEL and join["uid"] == 900001
    assert join["encryption"] == {"mode": "aes-256-gcm2", "key": KEY, "salt": SALT}
    assert subscribe == {"op": "subscribe", "uids": [1, 2]}  # the rear camera is not leased
    assert harness.spawns.muxers == []  # nothing before the first key frame


async def test_frames_feed_one_muxer_per_camera_from_the_first_key_frame(
    harness: Harness,
) -> None:
    _, connection = await _started(harness)
    await connection.send(
        frame(1, key=False, recv_ms=900),  # before any key frame: dropped
        frame(1, key=True, recv_ms=1000, capture=77),
        frame(1, key=False, recv_ms=1100),
        frame(1, key=False, recv_ms=1250),  # a late frame keeps its real interval
        frame(2, key=True, recv_ms=1010),
        frame(3, key=True, recv_ms=1020),  # rear: not leased
        frame(9, key=True, recv_ms=1030),  # unknown uid
    )
    await eventually(lambda: len(harness.spawns.muxers) == 2)
    await eventually(lambda: len(harness.spawns.transcoders) == 2)
    left = harness.spawns.muxers[0]
    assert left.args == tuple(
        muxer_args(ffmpeg="ffmpeg", output_url=SERVER.url(source_path(RID, "front_left")))
    )
    assert "-fflags" not in left.args  # nobuffer would drop the probed key frame
    await eventually(lambda: len(_pes(bytes(left.stdin.data))) == 4)
    pes = _pes(bytes(left.stdin.data))
    pts = [p.pts for p in pes]
    assert pts[1:] == [CLOCK, CLOCK + 9000, CLOCK + 9000 + 13500]  # from recv_mono_ns
    assert pes[1].payload.startswith(KEYFRAME)
    transcoder_inputs = [t.args[t.args.index("-i") + 1] for t in harness.spawns.transcoders]
    assert transcoder_inputs == [
        SERVER.url(source_path(RID, "front_left")),
        SERVER.url(source_path(RID, "front_right")),
    ]
    assert harness.failures == []


async def test_a_full_muxer_drops_deltas_until_the_next_key_frame(harness: Harness) -> None:
    _, connection = await _started(harness, agora_session())
    await connection.send(frame(1, key=True, recv_ms=0))
    await eventually(lambda: len(harness.spawns.muxers) == 1)
    muxer = harness.spawns.muxers[0]
    await eventually(lambda: len(_pes(bytes(muxer.stdin.data))) == 2)
    muxer.stdin.pending = 10 * 1024 * 1024  # ffmpeg does not read
    await connection.send(frame(1, key=False, recv_ms=100))
    await _settle()
    muxer.stdin.pending = 0
    await connection.send(frame(1, key=False, recv_ms=200), frame(1, key=True, recv_ms=300))
    await eventually(lambda: len(_pes(bytes(muxer.stdin.data))) == 3)
    assert _pes(bytes(muxer.stdin.data))[-1].random_access  # resumed on the key frame
    feed = harness.driver.runs[RID].feeds["front_left"]
    assert feed.frames_dropped == 2


async def test_a_muxer_that_exits_restarts_at_the_next_key_frame(harness: Harness) -> None:
    _, connection = await _started(harness)
    await connection.send(frame(1, key=True, recv_ms=0))
    await eventually(lambda: len(harness.spawns.muxers) == 1)
    harness.spawns.muxers[0].exit(1)
    await _settle()
    await connection.send(frame(1, key=False, recv_ms=100), frame(1, key=True, recv_ms=200))
    await eventually(lambda: len(harness.spawns.muxers) == 2)
    restarted = _pes(bytes(harness.spawns.muxers[1].stdin.data))
    assert restarted[0].payload == AUD[2]  # a new stream: lead delimiter first
    assert restarted[1].payload.startswith(KEYFRAME)
    assert restarted[1].pts > CLOCK  # the time line goes on
    assert harness.failures == []


async def test_a_crashed_bridge_fails_the_source_and_stops_cleanly(harness: Harness) -> None:
    source, connection = await _started(harness)
    await connection.send(frame(1, key=True, recv_ms=0))
    await eventually(lambda: len(harness.spawns.muxers) == 1)
    await connection.exit(1)
    await eventually(lambda: harness.failures == [source])
    # The session domain stops the failed source: nothing to leave, every camera stops.
    await harness.driver.stop(source)
    assert harness.spawns.muxers[0].stdin.closed
    assert connection.ops() == ["join", "subscribe"]


async def test_a_broken_record_fails_the_source(harness: Harness) -> None:
    source, connection = await _started(harness)
    await connection.send(struct.pack(">I", 0))
    await eventually(lambda: harness.failures == [source])


async def test_stop_leaves_the_channel_and_stops_every_camera(harness: Harness) -> None:
    source, connection = await _started(harness)
    await connection.send(frame(1, key=True, recv_ms=0))
    await eventually(lambda: len(harness.spawns.transcoders) == 1)
    await harness.driver.stop(source)
    assert connection.ops()[-1] == "leave"
    assert harness.spawns.muxers[0].stdin.closed
    assert harness.spawns.transcoders[0].returncode is not None
    assert harness.failures == []  # leaving is not a failure
    assert RID not in harness.driver.runs


async def test_update_subscribes_new_cameras_and_stops_removed_ones(harness: Harness) -> None:
    source, connection = await _started(harness)
    await connection.send(frame(1, key=True, recv_ms=0))
    await eventually(lambda: len(harness.spawns.muxers) == 1)
    await harness.driver.update(source, ("front_right", "rear"))
    await eventually(lambda: connection.lines[-1] == {"op": "subscribe", "uids": [2, 3]})
    assert harness.spawns.muxers[0].stdin.closed
    assert sorted(harness.driver.runs[RID].feeds) == ["front_right", "rear"]


async def test_renewal_runs_five_minutes_before_expiry_on_the_same_connection(
    harness: Harness,
) -> None:
    await _started(harness)
    await eventually(lambda: harness.sleeps == [3300.0])  # 1 h session, renewed at 55 min
    connection = harness.bridge.connections[0]
    harness.clock.advance(3300)
    harness.fresh.append(agora_session(token=fresh_token(2), uid=900002, expires_in=3300 + 3600))
    harness.wake.put_nowait(None)
    await eventually(lambda: connection.ops()[-1] == "renew")
    assert connection.lines[-1] == {"op": "renew", "token": fresh_token(2), "uid": 900002}
    await connection.event("renewed")
    await eventually(lambda: len(harness.sleeps) == 2)
    assert harness.sleeps[1] == 3300.0  # the next renewal follows the new expiry
    assert len(harness.bridge.connections) == 1


async def test_a_refused_renewal_rejoins_before_leaving_the_old_connection(
    harness: Harness,
) -> None:
    _, old = await _started(harness)
    await old.send(frame(1, key=True, recv_ms=0), frame(2, key=True, recv_ms=0))
    await eventually(lambda: len(harness.spawns.muxers) == 2)
    await eventually(lambda: harness.sleeps == [3300.0])
    harness.fresh.append(agora_session(token=fresh_token(3), uid=900003))
    harness.wake.put_nowait(None)
    await eventually(lambda: old.ops()[-1] == "renew")
    await old.event("renew_failed", reason="sdk_error_-1")
    new = await harness.bridge.connection(1)
    await eventually(lambda: new.ops() == ["join", "subscribe"])
    assert new.lines[0]["token"] == fresh_token(3) and new.lines[0]["uid"] == 900003
    # The old connection still feeds the cameras until the new one has a key frame for each.
    await new.send(frame(1, key=False, recv_ms=50_000))  # no key frame yet: ignored
    await old.send(frame(1, key=False, recv_ms=100))
    await new.send(frame(1, key=True, recv_ms=50_100))
    await _settle()
    assert "leave" not in old.ops()  # front_right still on the old connection
    await new.send(frame(2, key=True, recv_ms=50_150))
    await eventually(lambda: "leave" in old.ops())
    assert len(harness.spawns.muxers) == 2  # the same streams continue
    left = _pes(bytes(harness.spawns.muxers[0].stdin.data))
    pts = [p.pts for p in left[1:]]
    assert pts == sorted(pts) and len(set(pts)) == len(pts)  # one monotonic time line
    await asyncio.wait_for(old.closed.wait(), 3)  # the old bridge left and exited
    await _settle()
    assert harness.failures == []  # the left connection ending is no failure


async def test_a_camera_that_never_publishes_does_not_hold_up_a_rejoin(
    harness: Harness,
) -> None:
    source = _source(agora_session(), cameras=("front_left", "rear"))
    await harness.driver.start(source)
    old = await harness.bridge.connection(0)
    await old.send(frame(1, key=True, recv_ms=0))
    await eventually(lambda: len(harness.spawns.muxers) == 1)
    harness.fresh.append(agora_session(token=fresh_token(7)))
    await old.event("token_will_expire")
    new = await harness.bridge.connection(1)
    await eventually(lambda: new.ops() == ["join", "subscribe"])
    await new.send(frame(1, key=True, recv_ms=10))
    await eventually(lambda: "leave" in old.ops(), within=2)  # long before the 5 s deadline


async def test_the_sdk_expiry_warning_rejoins(harness: Harness) -> None:
    _, connection = await _started(harness)
    harness.fresh.append(agora_session(token=fresh_token(4)))
    await connection.event("token_will_expire")
    new = await harness.bridge.connection(1)
    await eventually(lambda: new.ops() == ["join", "subscribe"])
    assert new.lines[0]["token"] == fresh_token(4)


async def test_another_channel_rejoins_without_renew(harness: Harness) -> None:
    _, connection = await _started(harness)
    await eventually(lambda: harness.sleeps == [3300.0])
    harness.fresh.append(agora_session(token=fresh_token(5), channel="channel-2"))
    harness.wake.put_nowait(None)
    new = await harness.bridge.connection(1)
    await eventually(lambda: new.ops() == ["join", "subscribe"])
    assert "renew" not in connection.ops()
    assert new.lines[0]["channel"] == "channel-2"


async def test_the_old_connection_dying_during_a_rejoin_hands_over(harness: Harness) -> None:
    source, old = await _started(harness)
    harness.fresh.append(agora_session(token=fresh_token(6)))
    await old.event("token_will_expire")
    new = await harness.bridge.connection(1)
    await eventually(lambda: new.ops() == ["join", "subscribe"])
    await old.exit(1)
    await _settle()
    await new.send(frame(1, key=True, recv_ms=1))
    await eventually(lambda: len(harness.spawns.muxers) == 1)
    assert harness.failures == []
    assert harness.driver.runs[source.relay_id].active is not None


async def test_no_secret_reaches_logs_arguments_or_environment(harness: Harness) -> None:
    with structlog.testing.capture_logs() as logs:
        source, connection = await _started(harness)
        await connection.send(frame(1, key=True, recv_ms=0), frame(2, key=True, recv_ms=0))
        await connection.event("error", code="sdk_error", detail="code 17")
        await eventually(lambda: len(harness.spawns.transcoders) == 2)
        await harness.driver.stop(source)
    state = repr(source) + repr(harness.driver.runs)
    for secret in SECRETS:
        assert secret not in repr(logs)
        assert all(secret not in arg for arg in harness.spawns.all_args())
        assert all(secret not in value for value in os.environ.values())
    for secret in (TOKEN, KEY, SALT, APP_ID):
        assert secret not in state  # the channel is part of the release, not a secret
    assert any(entry["event"] == "agora bridge error" for entry in logs)


# --- Availability ------------------------------------------------------------------------------


async def test_available_only_while_the_bridge_listens(harness: Harness) -> None:
    assert await harness.driver.available()
    await eventually(lambda: harness.bridge.probes == 1)  # a probe starts no bridge
    assert harness.bridge.connections == []
    await harness.bridge.close()
    assert not await harness.driver.available()


async def test_without_a_reachable_bridge_agora_is_unsupported_and_not_released() -> None:
    releases: list[Any] = []

    async def on_session(message: BusMessage) -> None:
        await message.respond(CameraSessionReply(session=agora_session()))

    async def on_release(message: BusMessage) -> None:
        releases.append(message.model(CameraReleaseRequest))
        await message.respond(CameraReleaseReply(released=True))

    bus = InMemoryBus()
    await bus.subscribe(ALL_CAMERA_SESSIONS, on_session)
    await bus.subscribe(ALL_CAMERA_RELEASES, on_release)
    bridge = FakeBridgeServer()
    port = await bridge.start()
    driver = BridgeSourceDriver(
        endpoint=BridgeEndpoint("127.0.0.1", port),
        server=SERVER,
        ffmpeg="ffmpeg",
        on_failure=lambda source: None,
        fetch_session=lambda k: asyncio.sleep(0, agora_session()),
    )
    vendor = GatewayVendorSessions(
        lambda: bus,
        [driver],
        reports=lambda: None,  # type: ignore[arg-type, return-value]
        principal=service_principal(uuid4()),
    )
    offer = await vendor.offer(key())
    assert offer.provider is CameraProvider.AGORA
    await bridge.close()
    with pytest.raises(UnsupportedProviderError, match="agora"):
        await vendor.offer(key())
    assert releases == []  # the direct viewer of the fallback needs the stream
    without = GatewayVendorSessions(
        lambda: bus,
        [],
        reports=lambda: None,  # type: ignore[arg-type, return-value]
        principal=service_principal(uuid4()),
    )
    with pytest.raises(UnsupportedProviderError):
        await without.offer(key())


@pytest.mark.parametrize("url", ["http://bridge:7700", "tcp://bridge", "tcp://:7700"])
def test_the_endpoint_must_be_tcp_host_and_port(url: str) -> None:
    with pytest.raises(ValueError, match="tcp://host:port"):
        BridgeEndpoint.parse(url)
    assert str(BridgeEndpoint.parse("tcp://127.0.0.1:7700")) == "tcp://127.0.0.1:7700"


def test_renew_delay_keeps_the_lead_and_a_minimum() -> None:
    clock = FakeClock(T0)
    driver = BridgeSourceDriver(
        endpoint=BridgeEndpoint("127.0.0.1", 1),
        server=SERVER,
        ffmpeg="ffmpeg",
        on_failure=lambda source: None,
        fetch_session=lambda k: asyncio.sleep(0, agora_session()),
        now=clock,
    )
    assert driver.renew_delay(agora_session(expires_in=3600)) == 3300
    assert driver.renew_delay(agora_session(expires_in=120)) == 60  # half the time left
    clock.advance(4000)
    assert driver.renew_delay(agora_session(expires_in=3600)) == 5  # overdue: soon


async def test_a_start_that_fails_after_the_join_leaves_nothing_running(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    def refuse(link: Any, cameras: Any) -> bytes:
        raise ConnectionResetError

    monkeypatch.setattr(harness.driver, "_subscribe_line", refuse)
    source = _source(agora_session())
    with pytest.raises(ConnectionResetError):
        await harness.driver.start(source)
    assert RID not in harness.driver.runs
    connection = await harness.bridge.connection(0)
    await asyncio.wait_for(connection.closed.wait(), 3)  # the bridge process was told to leave


# --- Transcoder start: only once .../src is ready ----------------------------------------------


@dataclass
class FeedTime:
    """Virtual time of the camera feeds: every sleep moves it on at once."""

    now: float = 0.0
    ready_at: dict[str, float] = field(default_factory=dict)
    """Path -> virtual second from which the media server reports it ready."""
    asked: list[str] = field(default_factory=list)

    def monotonic(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.now += seconds
        await asyncio.sleep(0)

    async def ready(self, path: str) -> bool:
        self.asked.append(path)
        return self.now >= self.ready_at.get(path, float("inf"))


@pytest.fixture
async def timed(request: pytest.FixtureRequest) -> AsyncIterator[tuple[Harness, FeedTime]]:
    # The stall watch runs on the feeds' virtual time only where a test asks for it (indirect
    # parameter: the stall timeout): every other test sends a few frames only.
    stall_timeout: float | None = getattr(request, "param", None)
    bridge = FakeBridgeServer()
    port = await bridge.start()
    spawns = FakeSpawns()
    clock = FakeClock(T0)
    feed_time = FeedTime()
    holder: list[Harness] = []
    driver = BridgeSourceDriver(
        endpoint=BridgeEndpoint("127.0.0.1", port),
        server=SERVER,
        ffmpeg="ffmpeg",
        on_failure=lambda source: holder[0].failures.append(source),
        fetch_session=lambda k: holder[0].fetch(k),
        spawn=spawns.plain,
        spawn_stdin=spawns.stdin,
        now=clock,
        sleep=lambda s: holder[0].sleep(s),
        source_ready=feed_time.ready,
        ready_timeout=20,
        feed_monotonic=feed_time.monotonic,
        feed_sleep=feed_time.sleep,
        stall_timeout=stall_timeout,
        watch_sleep=feed_time.sleep,
    )
    h = Harness(bridge, spawns, driver, clock)
    holder.append(h)
    try:
        yield h, feed_time
    finally:
        for source in [run.source for run in list(driver.runs.values())]:
            await driver.stop(source)
        await bridge.close()


@pytest.mark.parametrize("ready_after", [4.5, 12.0])
async def test_the_transcoder_starts_only_once_the_source_path_is_ready(
    timed: tuple[Harness, FeedTime], ready_after: float
) -> None:
    # The LUBA's first key frame can come 4 s after the join; the muxer's probe and RTSP
    # handshake add more. A transcoder started before .../src exists only gets 404.
    h, feed_time = timed
    path = source_path(RID, "front_left")
    feed_time.ready_at[path] = ready_after
    _, connection = await _started(h)
    await connection.send(frame(1, key=True, recv_ms=0))
    await eventually(lambda: len(h.spawns.transcoders) == 1)
    assert ready_after <= feed_time.now < ready_after + 1  # polled, started once ready
    assert set(feed_time.asked) == {path}
    await _settle()
    assert len(h.spawns.transcoders) == 1  # no 404 retries, nothing failed
    feed = h.driver.runs[RID].feeds["front_left"]
    assert feed.transcoder_starts == 1 and not feed.source_late
    assert h.failures == []


async def test_a_late_source_is_logged_and_still_awaited_without_failing(
    timed: tuple[Harness, FeedTime],
) -> None:
    h, feed_time = timed
    feed_time.ready_at[source_path(RID, "front_left")] = 25.0
    _, connection = await _started(h)
    await connection.send(frame(1, key=True, recv_ms=0))
    await eventually(lambda: len(h.spawns.transcoders) == 1)
    assert feed_time.now >= 25.0
    assert h.driver.runs[RID].feeds["front_left"].source_late
    assert h.failures == []  # the session domain's start timeout decides, not the camera


async def test_an_exited_transcoder_is_started_again_with_a_backoff(
    timed: tuple[Harness, FeedTime],
) -> None:
    h, feed_time = timed
    path = source_path(RID, "front_left")
    feed_time.ready_at[path] = 0.0
    _, connection = await _started(h)
    await connection.send(frame(1, key=True, recv_ms=0))
    await eventually(lambda: len(h.spawns.transcoders) == 1)
    for attempt in range(1, 6):  # far more than the old three restarts
        before = feed_time.now
        h.spawns.transcoders[-1].exit(8)  # e.g. "Server returned 404 Not Found"
        await eventually(lambda n=attempt: len(h.spawns.transcoders) == n + 1)
        assert feed_time.now - before >= min(2 ** (attempt - 1), 30)
    assert h.failures == []


async def test_a_camera_that_never_publishes_blocks_and_fails_nothing(
    timed: tuple[Harness, FeedTime],
) -> None:
    h, feed_time = timed
    for camera in ("front_left", "front_right", "rear"):
        feed_time.ready_at[source_path(RID, camera)] = 1.0
    source = _source(agora_session(), ("front_left", "front_right", "rear"))
    await h.driver.start(source)
    connection = await h.bridge.connection(0)
    await eventually(lambda: connection.ops() == ["join", "subscribe"])
    assert connection.lines[1] == {"op": "subscribe", "uids": [1, 2, 3]}
    await connection.event("publisher", uid=3, state="published")  # listed, never sends
    await connection.send(frame(1, key=True, recv_ms=0), frame(2, key=True, recv_ms=5))
    await eventually(lambda: len(h.spawns.transcoders) == 2)
    feed_time.now += 120
    await _settle()
    rear = h.driver.runs[RID].feeds["rear"]
    assert (rear.frames_written, rear.transcoder_starts) == (0, 0)
    assert h.failures == []


def _muxers_of(h: Harness, camera: str) -> list[Any]:
    return [m for m in h.spawns.muxers if source_path(RID, camera) in m.args[-1]]


# --- Recovery (live incident 2026-10-18: a LUBA mowing over 4G) ---------------------------------


async def test_a_muxer_that_keeps_exiting_restarts_at_key_frames_with_a_backoff(
    timed: tuple[Harness, FeedTime],
) -> None:
    # The incident: the fourth muxer start within a minute was refused ("failed for good") and
    # the camera never came back. Now every exit restarts at a key frame: the first at once,
    # then after 1, 2, 4 ... s (capped at 30 s), and nothing fails.
    h, feed_time = timed
    feed_time.ready_at[source_path(RID, "front_left")] = 0.0  # virtual time moves by hand only
    _, connection = await _started(h)
    await connection.send(frame(1, key=True, recv_ms=0))
    await eventually(lambda: len(h.spawns.muxers) == 1)
    feed = h.driver.runs[RID].feeds["front_left"]
    expected = [0.0, 1.0, 2.0, 4.0, 8.0, 16.0, 30.0, 30.0]
    for n, delay in enumerate(expected, start=1):
        h.spawns.muxers[-1].exit(224)  # e.g. Broken pipe: MediaMTX dropped the publisher
        await eventually(lambda: feed.process is None or feed.process.returncode is not None)
        await _settle()
        exited_at = feed_time.now
        if delay:
            await connection.send(frame(1, key=True, recv_ms=1000 * n))  # inside the backoff
            await _settle()
            assert len(h.spawns.muxers) == n, f"restart {n} came before its backoff"
            feed_time.now = exited_at + delay
        await connection.send(frame(1, key=True, recv_ms=1000 * n + 500))
        await eventually(lambda n=n: len(h.spawns.muxers) == n + 1)
    assert not feed.failed and h.failures == []
    # A muxer that ran 30 s restarts at once again.
    feed_time.now += 31
    h.spawns.muxers[-1].exit(224)
    await _settle()
    await connection.send(frame(1, key=True, recv_ms=99_000))
    await eventually(lambda: len(h.spawns.muxers) == len(expected) + 2)


async def test_unpublished_stops_the_muxer_and_published_starts_a_new_one(
    timed: tuple[Harness, FeedTime],
) -> None:
    h, feed_time = timed
    path = source_path(RID, "front_left")
    feed_time.ready_at[path] = 0.0
    feed_time.ready_at[source_path(RID, "front_right")] = 0.0
    _, connection = await _started(h)
    await connection.send(frame(1, key=True, recv_ms=0), frame(2, key=True, recv_ms=0))
    await eventually(lambda: len(h.spawns.transcoders) == 2)
    feed = h.driver.runs[RID].feeds["front_left"]
    old = _muxers_of(h, "front_left")[0]

    await connection.event("publisher", uid=1, state="unpublished")
    await eventually(lambda: old.returncode is not None)
    assert old.stdin.closed  # stopped cleanly: stdin closed, no kill
    assert feed.process is None and not feed.publisher_present
    right = _muxers_of(h, "front_right")[0]
    assert right.returncode is None  # the other camera goes on
    await connection.send(frame(1, key=False, recv_ms=100))  # nothing until a key frame
    await _settle()
    assert len(_muxers_of(h, "front_left")) == 1

    # The source path went away: the transcoder exits and attaches again at the shortest delay.
    feed_time.ready_at[path] = float("inf")
    left_transcoder = h.spawns.transcoders[0]
    left_transcoder.exit(0)
    await connection.event("publisher", uid=1, state="published")
    await _settle()
    assert feed.publisher_present
    await connection.send(frame(1, key=False, recv_ms=4000), frame(1, key=True, recv_ms=4100))
    await eventually(lambda: len(_muxers_of(h, "front_left")) == 2)
    restarted = _pes(bytes(_muxers_of(h, "front_left")[1].stdin.data))
    assert restarted[1].payload.startswith(KEYFRAME)
    feed_time.ready_at[path] = feed_time.now
    before = feed_time.now
    await eventually(lambda: len(h.spawns.transcoders) == 3)
    assert feed_time.now - before < 2  # no long transcoder backoff after the source came back
    assert h.failures == []


async def test_publisher_events_of_the_other_connection_of_a_rejoin_change_nothing(
    harness: Harness,
) -> None:
    _, connection = await _started(harness)
    await connection.send(frame(1, key=True, recv_ms=0))
    await eventually(lambda: len(harness.spawns.muxers) == 1)
    run = harness.driver.runs[RID]
    pending = await harness.driver._open(run, agora_session())
    run.pending = pending
    second = await harness.bridge.connection(1)
    await second.event("publisher", uid=1, state="unpublished")
    await _settle()
    assert harness.spawns.muxers[0].returncode is None


@pytest.mark.parametrize("timed", [20.0], indirect=True)
async def test_a_silent_camera_gets_its_muxer_stopped_and_no_zombie_is_left(
    timed: tuple[Harness, FeedTime],
) -> None:
    # The incident: a camera whose frames stopped kept an ffmpeg waiting on a publish MediaMTX
    # had dropped, and it counted as "delivering", so the session never failed over either.
    h, feed_time = timed
    _, connection = await _started(h)
    await connection.send(frame(1, key=True, recv_ms=0))
    await eventually(lambda: len(h.spawns.muxers) == 1)
    feed = h.driver.runs[RID].feeds["front_left"]
    muxer = h.spawns.muxers[0]
    await eventually(lambda: muxer.returncode is not None)  # the watch's virtual 20 s
    assert muxer.stdin.closed and feed.process is None
    assert feed.stalls == 1 and not feed.delivering
    assert feed_time.now >= 20
    # Frames again: a new muxer from the next key frame on (the stop is no muxer fault).
    await connection.send(frame(1, key=False, recv_ms=30_000), frame(1, key=True, recv_ms=30_100))
    await eventually(lambda: len(h.spawns.muxers) == 2)
    assert h.failures == []


async def test_a_muxer_that_cannot_start_spares_the_source_while_another_camera_delivers(
    timed: tuple[Harness, FeedTime],
) -> None:
    h, _ = timed
    _, connection = await _started(h)
    await connection.send(frame(1, key=True, recv_ms=0))
    await eventually(lambda: len(h.spawns.muxers) == 1)
    h.spawns.fail_stdin = True  # no ffmpeg any more
    await connection.send(frame(1, key=False, recv_ms=100), frame(2, key=True, recv_ms=100))
    await eventually(lambda: h.driver.runs[RID].feeds["front_right"].failed)
    await _settle()
    assert h.failures == []  # the left camera delivers


async def test_a_muxer_that_cannot_start_fails_the_source_when_the_others_are_silent(
    timed: tuple[Harness, FeedTime],
) -> None:
    # The incident's second half: the other camera's muxer was alive but had not had a frame
    # for minutes, and it counted as delivering, so the session never failed over.
    h, feed_time = timed
    source, connection = await _started(h)
    await connection.send(frame(1, key=True, recv_ms=0))
    await eventually(lambda: len(h.spawns.muxers) == 1)
    feed_time.now += 60  # the left camera falls silent; its muxer still runs
    h.spawns.fail_stdin = True
    await connection.send(frame(2, key=True, recv_ms=60_000))
    await eventually(lambda: h.failures == [source])


def test_a_deeply_nested_bridge_event_is_a_protocol_error() -> None:
    # The relay's reader of the bridge's records (same finding as the bridge's own reader).
    from camera_relay.adapters.media.bridge_protocol import (
        RECORD_EVENT,
        BridgeProtocolError,
        decode_record,
    )

    with pytest.raises(BridgeProtocolError):
        decode_record(bytes([RECORD_EVENT]) + b"[" * 100_000)
