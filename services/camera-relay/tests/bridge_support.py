"""Fakes for the bridge source tests: a bridge server on loopback and ffmpeg processes."""

import asyncio
import contextlib
import json
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

from mowdeck_contracts.v1 import CameraSession
from mowdeck_contracts.v1.camera import CameraEncryption, CameraInfo, CameraProvider
from relay_support import T0

from camera_relay.adapters.media.bridge_protocol import (
    CODEC_H265,
    BridgeFrame,
    encode_event,
    encode_frame,
)

TOKEN = "007eJxAGORA-TOKEN-SECRET"  # noqa: S105 - a test value
KEY = "encryption-KEY-SECRET"
SALT = "c2FsdC1TRUNSRVQtc2FsdC1TRUNSRVQtc2FsdC1TRUNS"
APP_ID = "agora-APP-ID-SECRET"
CHANNEL = "channel-SECRET-1"
SECRETS = (TOKEN, KEY, SALT, APP_ID, CHANNEL)

KEYFRAME = b"\x00\x00\x00\x01\x40\x01VPS\x00\x00\x00\x01\x42\x01SPS\x00\x00\x00\x01\x26\x01IDR"
DELTA = b"\x00\x00\x00\x01\x02\x01TRAIL"


def fresh_token(n: int) -> str:
    """The token of the n-th renewed session."""
    return f"fresh-TOKEN-{n}"


def agora_session(
    *,
    token: str = TOKEN,
    channel: str = CHANNEL,
    uid: int = 900001,
    expires_in: float = 3600,
    encrypted: bool = True,
) -> CameraSession:
    """A vendor session like Mammotion's: three cameras, the rear one never publishes."""
    return CameraSession(
        provider=CameraProvider.AGORA,
        app_id=APP_ID,
        channel=channel,
        token=token,
        uid=uid,
        cameras=[
            CameraInfo(id="front_left", name="Front left", uid=1),
            CameraInfo(id="front_right", name="Front right", uid=2),
            CameraInfo(id="rear", name="Rear", uid=3),
        ],
        encryption=CameraEncryption(mode="aes-256-gcm2", key=KEY, salt=SALT) if encrypted else None,
        expires_at=T0 + timedelta(seconds=expires_in),
    )


def frame(uid: int, *, key: bool, recv_ms: int, capture: int | None = None) -> bytes:
    """A frame record."""
    return encode_frame(
        BridgeFrame(
            uid=uid,
            codec=CODEC_H265,
            keyframe=key,
            recv_mono_ns=recv_ms * 1_000_000,
            capture_ts=capture,
            data=KEYFRAME if key else DELTA,
        )
    )


@dataclass
class BridgeConnection:
    """One connection the relay opened (one bridge process)."""

    reader: asyncio.StreamReader
    writer: asyncio.StreamWriter
    lines: list[dict[str, Any]] = field(default_factory=list)
    raw: bytearray = field(default_factory=bytearray)
    closed: asyncio.Event = field(default_factory=asyncio.Event)
    task: asyncio.Task[None] | None = None

    async def read_lines(self, *, auto_leave: bool = True) -> None:
        while line := await self.reader.readline():
            self.raw += line
            self.lines.append(json.loads(line))
            if auto_leave and self.lines[-1]["op"] == "leave":
                # The bridge leaves and exits; the server sends the exit record and closes.
                with contextlib.suppress(OSError, ConnectionError):
                    await self.event("left")
                    await self.event("exit", code=0)
                    self.writer.close()
                break
        self.closed.set()

    def ops(self) -> list[str]:
        return [line["op"] for line in self.lines]

    async def send(self, *records: bytes) -> None:
        for record in records:
            self.writer.write(record)
        await self.writer.drain()

    async def event(self, name: str, **fields: Any) -> None:
        await self.send(encode_event(name, **fields))

    async def exit(self, code: int) -> None:
        """The bridge process ended: the server's exit record, then the connection closes."""
        await self.event("exit", code=code)
        self.writer.close()
        with contextlib.suppress(OSError, ConnectionError):
            await self.writer.wait_closed()


class FakeBridgeServer:
    """The agora-bridge server's socket transport as data: records lines, sends records."""

    def __init__(self, *, auto_leave: bool = True) -> None:
        self.connections: list[BridgeConnection] = []
        self.probes = 0
        self.auto_leave = auto_leave
        self._server: asyncio.Server | None = None
        self.port = 0

    async def start(self) -> int:
        self._server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        self.port = self._server.sockets[0].getsockname()[1]
        return self.port

    async def close(self) -> None:
        for connection in self.connections:
            connection.writer.close()
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        first = await reader.readline()
        if not first:
            self.probes += 1
            writer.close()
            return
        connection = BridgeConnection(reader, writer)
        connection.raw += first
        connection.lines.append(json.loads(first))
        self.connections.append(connection)
        await connection.event("hello", protocol=1, bridge="fake", sdk="fake-sdk")
        await connection.read_lines(auto_leave=self.auto_leave)

    async def connection(self, index: int, within: float = 3.0) -> BridgeConnection:
        await eventually(lambda: len(self.connections) > index, within)
        return self.connections[index]


async def eventually(check: Any, within: float = 3.0) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + within
    while not check():
        if loop.time() > deadline:
            msg = "condition not reached"
            raise AssertionError(msg)
        await asyncio.sleep(0.01)


class FakeTransport:
    def __init__(self, writer: "FakeStdin") -> None:
        self._writer = writer

    def get_write_buffer_size(self) -> int:
        return self._writer.pending


@dataclass
class FakeStdin:
    data: bytearray = field(default_factory=bytearray)
    pending: int = 0
    closed: bool = False

    def __post_init__(self) -> None:
        self.transport = FakeTransport(self)

    def write(self, chunk: bytes) -> None:
        self.data += chunk

    def is_closing(self) -> bool:
        return self.closed

    def close(self) -> None:
        self.closed = True


@dataclass
class FakeMuxer:
    """A muxer process: what arrives on its stdin."""

    pid: int
    args: tuple[str, ...]
    returncode: int | None = None
    stdin: FakeStdin = field(default_factory=FakeStdin)
    stderr: asyncio.StreamReader = field(default_factory=asyncio.StreamReader)
    stdout: asyncio.StreamReader = field(default_factory=asyncio.StreamReader)
    _exited: asyncio.Event = field(default_factory=asyncio.Event)

    def exit(self, code: int) -> None:
        self.returncode = code
        self.stderr.feed_eof()
        self.stdout.feed_eof()
        self._exited.set()

    async def wait(self) -> int:
        if self.stdin.closed and self.returncode is None:
            self.exit(0)
        await self._exited.wait()
        assert self.returncode is not None
        return self.returncode

    def terminate(self) -> None:
        self.exit(-15)

    def kill(self) -> None:
        self.exit(-9)

    def send_signal(self, sig: int) -> None:
        del sig


@dataclass
class FakeSpawns:
    """Both spawns: muxers (stdin) and transcoders (SupervisedProcess)."""

    muxers: list[FakeMuxer] = field(default_factory=list)
    transcoders: list[FakeMuxer] = field(default_factory=list)

    async def stdin(self, *args: str) -> Any:
        process = FakeMuxer(pid=500 + len(self.muxers), args=args)
        self.muxers.append(process)
        return process

    async def plain(self, *args: str) -> Any:
        process = FakeMuxer(pid=700 + len(self.transcoders), args=args)
        self.transcoders.append(process)
        return process

    def all_args(self) -> list[str]:
        return [arg for p in [*self.muxers, *self.transcoders] for arg in p.args]
