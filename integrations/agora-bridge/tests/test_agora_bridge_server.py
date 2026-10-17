# SPDX-License-Identifier: Apache-2.0
"""The TCP server: one bridge process per connection, bytes copied verbatim, the exit record."""

import asyncio
import json
import sys
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from agora_bridge.protocol import Frame, RecordReader
from agora_bridge.server import BridgeServer, run_check

TOKEN = "007eJx-server-test-SECRET"  # noqa: S105 - a test value

# A stand-in bridge: answers every line with an "echo" event (the line's length), writes its
# argv and environment to FAKE_BRIDGE_DUMP, exits with 3 on a line containing "crash", and
# leaves (event "left", code 0) when stdin closes. "--check" exits with FAKE_CHECK_CODE.
FAKE_BRIDGE = r"""
import json, os, struct, sys
def record(obj):
    body = b"\x02" + json.dumps(obj).encode()
    sys.stdout.buffer.write(struct.pack(">I", len(body)) + body)
    sys.stdout.buffer.flush()
if "--check" in sys.argv:
    sys.exit(int(os.environ.get("FAKE_CHECK_CODE", "0")))
dump = os.environ.get("FAKE_BRIDGE_DUMP")
if dump:
    with open(dump, "a") as f:
        f.write(json.dumps({"argv": sys.argv, "env": dict(os.environ)}) + "\n")
print("bridge log line", file=sys.stderr, flush=True)
for line in sys.stdin:
    record({"event": "echo", "n": len(line)})
    if "crash" in line:
        sys.exit(3)
record({"event": "left"})
"""


@pytest.fixture
def fake_bridge(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> list[str]:
    script = tmp_path / "fake_bridge.py"
    script.write_text(FAKE_BRIDGE, "utf-8")
    monkeypatch.setenv("FAKE_BRIDGE_DUMP", str(tmp_path / "dump.jsonl"))
    return [sys.executable, str(script)]


async def _serve(server: BridgeServer) -> AsyncIterator[int]:
    tcp = await asyncio.start_server(server.handle, "127.0.0.1", 0)
    port = tcp.sockets[0].getsockname()[1]
    async with tcp:
        yield port
        tcp.close()


async def _records(reader: asyncio.StreamReader) -> list[Frame | dict[str, Any]]:
    parser, records = RecordReader(), []
    while chunk := await asyncio.wait_for(reader.read(65536), 10):
        records += parser.feed(chunk)
    return records


async def test_a_session_copies_both_ways_and_ends_with_the_exit_record(
    fake_bridge: list[str], tmp_path: Path
) -> None:
    server = BridgeServer(fake_bridge)
    async for port in _serve(server):
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(json.dumps({"op": "join", "token": TOKEN}).encode() + b"\n")
        writer.write(b'{"op": "subscribe", "uids": [1]}\n')
        await writer.drain()
        writer.write_eof()  # the relay is done: stdin closes, the bridge leaves
        records = await _records(reader)
        writer.close()
    assert [r["event"] for r in records if isinstance(r, dict)] == ["echo", "echo", "left", "exit"]
    assert records[-1] == {"event": "exit", "code": 0}
    dumped = (tmp_path / "dump.jsonl").read_text("utf-8")
    assert TOKEN not in dumped  # never in argv or environment: stdin only
    assert server.active == 0


async def test_a_crashing_bridge_reports_its_exit_code(fake_bridge: list[str]) -> None:
    server = BridgeServer(fake_bridge)
    async for port in _serve(server):
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(b'{"op": "crash"}\n')
        await writer.drain()
        records = await _records(reader)
        writer.close()
    assert records[-1] == {"event": "exit", "code": 3}


async def test_a_probe_starts_no_bridge(fake_bridge: list[str], tmp_path: Path) -> None:
    server = BridgeServer(fake_bridge, first_line_timeout=5)
    async for port in _serve(server):
        _, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.close()
        await writer.wait_closed()
        await asyncio.sleep(0.2)
    assert not (tmp_path / "dump.jsonl").exists()


async def test_sessions_beyond_the_limit_are_refused(fake_bridge: list[str]) -> None:
    server = BridgeServer(fake_bridge, max_sessions=1)
    async for port in _serve(server):
        first_reader, first = await asyncio.open_connection("127.0.0.1", port)
        first.write(b'{"op": "join"}\n')
        await first.drain()
        await asyncio.wait_for(first_reader.read(4), 10)  # the first bridge is running
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(b'{"op": "join"}\n')
        await writer.drain()
        records = await _records(reader)
        writer.close()
        first.close()
    assert records == [
        {"event": "error", "code": "busy", "detail": "too many sessions"},
        {"event": "exit", "code": 1},
    ]


async def test_a_vanished_relay_closes_the_bridge_stdin(fake_bridge: list[str]) -> None:
    server = BridgeServer(fake_bridge)
    async for port in _serve(server):
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(b'{"op": "join"}\n')
        await writer.drain()
        await asyncio.wait_for(reader.read(4), 10)
        writer.close()
        for _ in range(100):
            if server.active == 0:
                break
            await asyncio.sleep(0.05)
    assert server.active == 0


async def test_the_check_decides_whether_the_server_listens(
    fake_bridge: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    assert await run_check(fake_bridge)
    monkeypatch.setenv("FAKE_CHECK_CODE", "3")
    assert not await run_check(fake_bridge)
