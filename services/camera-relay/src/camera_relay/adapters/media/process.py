"""A supervised ffmpeg process: start, stop, pause, progress and restarts.

Every process of the source pipeline (a fake source, a transcoder) runs as a
:class:`SupervisedProcess`. It reads ffmpeg's machine readable ``-progress pipe:1`` output into
:class:`ProgressStats` (frames written, duplicated and dropped frames, speed) and logs stderr
with every secret replaced (the RTSP URLs carry the internal publisher credential). An
unexpected exit is restarted after a short delay, at most ``max_restarts`` times within
``restart_window`` seconds; then ``on_failure`` is called once and the process stays down.

:meth:`SupervisedProcess.pause` stops the process with ``SIGSTOP`` (Linux): no frame leaves it
until :meth:`SupervisedProcess.resume`. The test hook ``POST /internal/v1/test/freeze`` uses it
to simulate a frozen source.
"""

import asyncio
import contextlib
import signal
import time
from collections import deque
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from mowdeck_service_kit import get_logger

_log = get_logger("camera_relay.media.process")

REDACTED = "<redacted>"


class Process(Protocol):
    """What the supervisor needs of ``asyncio.subprocess.Process``."""

    @property
    def pid(self) -> int:
        """The process id."""
        ...

    @property
    def returncode(self) -> int | None:
        """The exit code, None while running."""
        ...

    @property
    def stdout(self) -> asyncio.StreamReader | None:
        """ffmpeg's ``-progress pipe:1`` output."""
        ...

    @property
    def stderr(self) -> asyncio.StreamReader | None:
        """ffmpeg's log."""
        ...

    async def wait(self) -> int:
        """Wait for the exit and return the exit code."""
        ...

    def terminate(self) -> None:
        """Ask the process to exit (SIGTERM)."""
        ...

    def kill(self) -> None:
        """End the process (SIGKILL)."""
        ...

    def send_signal(self, sig: int) -> None:
        """Send a signal."""
        ...


class Spawn(Protocol):
    """Starts a process (``asyncio.create_subprocess_exec`` with pipes)."""

    async def __call__(self, *args: str) -> Process:
        """Start ``args``."""
        ...


async def spawn_process(*args: str) -> Process:
    """Start ``args`` with stdin closed and stdout/stderr piped."""
    process = await asyncio.create_subprocess_exec(
        *args,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    return process


@dataclass(slots=True)
class ProgressStats:
    """The last ``-progress`` block of ffmpeg (counted since the process started)."""

    frames: int = 0
    """Frames written to the output (``frame``)."""
    dup_frames: int = 0
    """Frames ffmpeg duplicated (must stay 0: the relay never repeats a frame)."""
    drop_frames: int = 0
    """Frames ffmpeg dropped."""
    fps: float = 0.0
    speed: str = ""
    updated_at: float | None = None
    """``time.monotonic()`` of the last block."""

    def apply(self, key: str, value: str) -> None:
        """Take one ``key=value`` line of the progress output."""
        try:
            match key:
                case "frame":
                    self.frames = int(value)
                case "dup_frames":
                    self.dup_frames = int(value)
                case "drop_frames":
                    self.drop_frames = int(value)
                case "fps":
                    self.fps = float(value)
                case "speed":
                    self.speed = value.strip()
                case "progress":
                    self.updated_at = time.monotonic()
        except ValueError:
            return

    def as_dict(self) -> dict[str, Any]:
        """JSON-ready copy."""
        return {
            "frames": self.frames,
            "dup_frames": self.dup_frames,
            "drop_frames": self.drop_frames,
            "fps": self.fps,
            "speed": self.speed,
        }


def redact(text: str, secrets: Sequence[str]) -> str:
    """``text`` with every non-empty secret replaced."""
    for secret in secrets:
        if secret:
            text = text.replace(secret, REDACTED)
    return text


class SupervisedProcess:
    """One long-running pipeline process with restarts and progress statistics."""

    def __init__(
        self,
        name: str,
        args: Sequence[str],
        *,
        secrets: Sequence[str] = (),
        on_failure: Callable[[], None] | None = None,
        spawn: Spawn = spawn_process,
        max_restarts: int = 3,
        restart_window: float = 60.0,
        restart_delay: float = 1.0,
        stop_timeout: float = 5.0,
    ) -> None:
        self.name = name
        self.args = tuple(args)
        self._secrets = tuple(secrets)
        self._on_failure = on_failure
        self._spawn = spawn
        self._max_restarts = max_restarts
        self._restart_window = restart_window
        self._restart_delay = restart_delay
        self._stop_timeout = stop_timeout
        self._process: Process | None = None
        self._task: asyncio.Task[None] | None = None
        self._stopping = False
        self._restarts: deque[float] = deque()
        self.paused = False
        self.failed = False
        self.starts = 0
        self.stats = ProgressStats()
        self.last_error = ""

    @property
    def running(self) -> bool:
        """The process is alive (or being restarted) and not stopped."""
        return self._task is not None and not self._task.done()

    @property
    def pid(self) -> int | None:
        """The current process id."""
        return self._process.pid if self._process is not None else None

    def describe(self) -> str:
        """The command line with secrets replaced (for logs)."""
        return redact(" ".join(self.args), self._secrets)

    async def start(self) -> None:
        """Start the process and its supervision; returns once it was spawned."""
        if self.running:
            return
        self._stopping = False
        self.failed = False
        self._process = await self._spawn_once()
        self._task = asyncio.create_task(self._supervise(), name=f"ffmpeg-{self.name}")

    async def stop(self) -> None:
        """Stop the process (SIGTERM, then SIGKILL after the timeout). Idempotent."""
        self._stopping = True
        process = self._process
        if process is not None and process.returncode is None:
            if self.paused:
                self._signal(process, "SIGCONT")
                self.paused = False
            with contextlib.suppress(ProcessLookupError):
                process.terminate()
            try:
                await asyncio.wait_for(process.wait(), self._stop_timeout)
            except TimeoutError:
                with contextlib.suppress(ProcessLookupError):
                    process.kill()
                await process.wait()
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None

    def pause(self) -> bool:
        """Freeze the process (SIGSTOP): it writes nothing until :meth:`resume`."""
        process = self._process
        if process is None or process.returncode is not None or self.paused:
            return False
        self._signal(process, "SIGSTOP")
        self.paused = True
        return True

    def resume(self) -> bool:
        """Continue a frozen process (SIGCONT)."""
        process = self._process
        if process is None or process.returncode is not None or not self.paused:
            return False
        self._signal(process, "SIGCONT")
        self.paused = False
        return True

    @staticmethod
    def _signal(process: Process, name: str) -> None:
        try:
            sig = signal.Signals[name]
        except KeyError as exc:  # not on Windows
            msg = f"{name} is not available on this platform"
            raise NotImplementedError(msg) from exc
        with contextlib.suppress(ProcessLookupError):
            process.send_signal(sig)

    async def _spawn_once(self) -> Process:
        self.starts += 1
        self.stats = ProgressStats()
        process = await self._spawn(*self.args)
        _log.info("pipeline process started", process=self.name, pid=process.pid)
        return process

    async def _supervise(self) -> None:
        while True:
            process = self._process
            assert process is not None  # noqa: S101 - set before the task starts
            readers = [
                asyncio.create_task(self._read_progress(process)),
                asyncio.create_task(self._read_stderr(process)),
            ]
            code = await process.wait()
            for reader in readers:
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(reader, 1.0)
            self.paused = False
            if self._stopping:
                return
            _log.warning(
                "pipeline process exited",
                process=self.name,
                code=code,
                error=self.last_error[-300:],
            )
            if not self._may_restart():
                self.failed = True
                _log.error("pipeline process failed for good", process=self.name)
                if self._on_failure is not None:
                    self._on_failure()
                return
            await asyncio.sleep(self._restart_delay)
            if self._stopping:
                return
            try:
                self._process = await self._spawn_once()
            except OSError:
                _log.exception("pipeline process could not be restarted", process=self.name)
                self.failed = True
                if self._on_failure is not None:
                    self._on_failure()
                return

    def _may_restart(self) -> bool:
        now = time.monotonic()
        while self._restarts and now - self._restarts[0] > self._restart_window:
            self._restarts.popleft()
        if len(self._restarts) >= self._max_restarts:
            return False
        self._restarts.append(now)
        return True

    async def _read_progress(self, process: Process) -> None:
        if process.stdout is None:
            return
        while line := await process.stdout.readline():
            text = line.decode("utf-8", "replace").strip()
            key, sep, value = text.partition("=")
            if sep:
                self.stats.apply(key.strip(), value)

    async def _read_stderr(self, process: Process) -> None:
        if process.stderr is None:
            return
        while line := await process.stderr.readline():
            text = redact(line.decode("utf-8", "replace").rstrip(), self._secrets)
            if text:
                self.last_error = text
                _log.debug("ffmpeg", process=self.name, line=text[:500])
