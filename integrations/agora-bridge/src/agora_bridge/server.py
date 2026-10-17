# SPDX-License-Identifier: Apache-2.0
"""``python -m agora_bridge.server``: the bridge protocol over TCP, one process per connection.

The camera relay runs in its own container and must never contain the Agora SDK, so it cannot
start the bridge as a child process. This server runs next to it (a sidecar sharing the relay's
network namespace: Compose ``network_mode: service:camera-relay``, a second container in the
relay's pod in Helm) and listens on loopback only by default:

* every accepted connection is one bridge session: the server starts ``python -m agora_bridge``
  when the first control line arrives (a connection that closes before is a reachability
  probe and starts nothing), and copies bytes verbatim, connection -> the bridge's stdin and the
  bridge's stdout -> connection; stderr goes to this server's log with a session number;
* when the bridge exits, the server appends one event record ``{"event": "exit", "code": N}``
  (the socket transport's only addition to the protocol) and closes the connection;
* when the relay closes the connection, the bridge's stdin is closed, which means ``leave``;
  a bridge that has not exited 10 s later is killed.

Before it listens, the server runs the bridge's ``--check`` (the SDK loads offline and TLS
verification is intact) and exits with code 3 when that fails, so a broken image is never
reachable and the relay keeps answering ``unsupported_provider``.

The server never parses a control line and logs none: secrets pass through it in memory only.
"""

import argparse
import asyncio
import contextlib
import itertools
import logging
import os
import shlex
import signal
import sys
from collections.abc import Sequence

from agora_bridge.protocol import MAX_LINE, encode_event

_log = logging.getLogger("agora_bridge.server")

DEFAULT_LISTEN = "127.0.0.1:7700"
DEFAULT_MAX_SESSIONS = 4
FIRST_LINE_TIMEOUT = 15.0
STOP_TIMEOUT = 10.0
CHECK_TIMEOUT = 120.0
CHUNK = 64 * 1024


class BridgeServer:
    """Accepts relay connections and runs one bridge process per connection."""

    def __init__(
        self,
        command: Sequence[str],
        *,
        max_sessions: int = DEFAULT_MAX_SESSIONS,
        first_line_timeout: float = FIRST_LINE_TIMEOUT,
        stop_timeout: float = STOP_TIMEOUT,
    ) -> None:
        self.command = list(command)
        self.max_sessions = max_sessions
        self.first_line_timeout = first_line_timeout
        self.stop_timeout = stop_timeout
        self.active = 0
        self.processes: set[asyncio.subprocess.Process] = set()
        self._ids = itertools.count(1)

    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        """One connection: wait for the first line, then run a bridge for it."""
        session = next(self._ids)
        try:
            first = await asyncio.wait_for(reader.readuntil(b"\n"), timeout=self.first_line_timeout)
        except (TimeoutError, asyncio.IncompleteReadError, asyncio.LimitOverrunError, OSError):
            await _close(writer)
            return
        if self.active >= self.max_sessions:
            _log.warning("session %d refused: %d sessions running", session, self.active)
            with contextlib.suppress(OSError):
                writer.write(encode_event("error", code="busy", detail="too many sessions"))
                writer.write(encode_event("exit", code=1))
                await writer.drain()
            await _close(writer)
            return
        self.active += 1
        try:
            await self._run(session, first, reader, writer)
        finally:
            self.active -= 1

    async def _run(
        self,
        session: int,
        first: bytes,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        try:
            process = await asyncio.create_subprocess_exec(
                *self.command,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except OSError:
            _log.exception("session %d: the bridge could not be started", session)
            with contextlib.suppress(OSError, ConnectionError):
                writer.write(encode_event("error", code="internal", detail="bridge not started"))
                writer.write(encode_event("exit", code=1))
                await writer.drain()
            await _close(writer)
            return
        self.processes.add(process)
        _log.info("session %d: bridge started (pid %d)", session, process.pid)
        assert process.stdin and process.stdout and process.stderr  # noqa: S101 - piped above
        inbound = asyncio.create_task(self._inbound(first, reader, process))
        logs = asyncio.create_task(_log_lines(session, process.stderr))
        outbound_ok = await _copy(process.stdout, writer)
        code = await process.wait()
        self.processes.discard(process)
        inbound.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await inbound
        with contextlib.suppress(Exception):
            await asyncio.wait_for(logs, 2.0)
        _log.info("session %d: bridge exited with %d", session, code)
        if outbound_ok:
            with contextlib.suppress(OSError):
                writer.write(encode_event("exit", code=code))
                await writer.drain()
        await _close(writer)

    async def _inbound(
        self, first: bytes, reader: asyncio.StreamReader, process: asyncio.subprocess.Process
    ) -> None:
        """Relay -> the bridge's stdin; the relay's end of the connection closes stdin."""
        stdin = process.stdin
        assert stdin is not None  # noqa: S101 - piped
        try:
            stdin.write(first)
            await stdin.drain()
            while chunk := await reader.read(CHUNK):
                stdin.write(chunk)
                await stdin.drain()
        except (OSError, ConnectionError):
            pass
        finally:
            with contextlib.suppress(OSError, ConnectionError):
                stdin.close()
        await self._await_exit(process)

    async def _await_exit(self, process: asyncio.subprocess.Process) -> None:
        """stdin is closed, so the bridge leaves; kill it when it does not in time."""
        try:
            await asyncio.wait_for(process.wait(), self.stop_timeout)
        except TimeoutError:
            with contextlib.suppress(ProcessLookupError):
                process.kill()

    async def shutdown(self) -> None:
        """Close every bridge's stdin (``leave``) and wait for them."""
        for process in list(self.processes):
            if process.stdin is not None:
                with contextlib.suppress(OSError, ConnectionError):
                    process.stdin.close()
        await asyncio.gather(
            *(self._await_exit(process) for process in list(self.processes)),
            return_exceptions=True,
        )


async def _copy(source: asyncio.StreamReader, writer: asyncio.StreamWriter) -> bool:
    """The bridge's stdout -> relay until EOF; False when the relay went away."""
    try:
        while chunk := await source.read(CHUNK):
            writer.write(chunk)
            await writer.drain()
    except (OSError, ConnectionError):
        # Keep draining stdout so the bridge never blocks on a full pipe while it leaves.
        while await source.read(CHUNK):
            pass
        return False
    return True


async def _log_lines(session: int, stderr: asyncio.StreamReader) -> None:
    while line := await stderr.readline():
        text = line.decode("utf-8", "replace").rstrip()
        if text:
            _log.info("session %d: %s", session, text[:500])


async def _close(writer: asyncio.StreamWriter) -> None:
    with contextlib.suppress(OSError, ConnectionError):
        writer.close()
        await writer.wait_closed()


async def run_check(command: Sequence[str], limit_seconds: float = CHECK_TIMEOUT) -> bool:
    """Run ``<command> --check``; True when it exits with 0."""
    process = await asyncio.create_subprocess_exec(
        *command, "--check", stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    try:
        out, err = await asyncio.wait_for(process.communicate(), limit_seconds)
    except TimeoutError:
        process.kill()
        _log.error("the bridge check did not finish in %.0f s", limit_seconds)
        return False
    for line in (out + err).decode("utf-8", "replace").splitlines():
        if line.strip():
            _log.info("check: %s", line[:500])
    return process.returncode == 0


def _address(listen: str) -> tuple[str, int]:
    host, sep, port = listen.rpartition(":")
    if not sep or not port.isdigit():
        msg = f"--listen must be host:port, not {listen!r}"
        raise SystemExit(msg)
    return host.strip("[]") or "127.0.0.1", int(port)


async def serve(args: argparse.Namespace) -> int:
    command = shlex.split(args.command) if args.command else [sys.executable, "-m", "agora_bridge"]
    if not args.skip_check and not await run_check(command):
        _log.error("the bridge cannot load its SDK: not listening")
        return 3
    server = BridgeServer(command, max_sessions=args.max_sessions)
    host, port = _address(args.listen)
    tcp = await asyncio.start_server(server.handle, host, port, limit=MAX_LINE + 2)
    _log.info("listening on %s:%d (at most %d sessions)", host, port, args.max_sessions)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for name in ("SIGTERM", "SIGINT"):
        sig = getattr(signal, name, None)
        if sig is not None:
            with contextlib.suppress(NotImplementedError, RuntimeError):
                loop.add_signal_handler(sig, stop.set)
    async with tcp:
        await stop.wait()
        _log.info("stopping: every bridge leaves its channel")
        tcp.close()
        await server.shutdown()
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="agora-bridge-server", description=__doc__.splitlines()[0]
    )
    parser.add_argument(
        "--listen",
        default=os.environ.get("AGORA_BRIDGE_LISTEN", DEFAULT_LISTEN),
        help=f"host:port (default {DEFAULT_LISTEN}, env AGORA_BRIDGE_LISTEN)",
    )
    parser.add_argument(
        "--max-sessions",
        type=int,
        default=int(os.environ.get("AGORA_BRIDGE_MAX_SESSIONS", DEFAULT_MAX_SESSIONS)),
        help="concurrent bridge processes (env AGORA_BRIDGE_MAX_SESSIONS)",
    )
    parser.add_argument(
        "--command",
        default=os.environ.get("AGORA_BRIDGE_COMMAND", ""),
        help="bridge command line (default: this Python with -m agora_bridge; tests replace it)",
    )
    parser.add_argument("--skip-check", action="store_true", help="do not run --check first")
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=os.environ.get("AGORA_BRIDGE_LOG_LEVEL", "INFO").upper(),
        stream=sys.stderr,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    try:
        return asyncio.run(serve(args))
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())
