"""The source pipeline: ffmpeg command lines, the supervised process and the fake source."""

import asyncio
import signal
import sys
from dataclasses import dataclass, field
from uuid import UUID, uuid4

import pytest
from mowdeck_contracts.v1.camera import CameraProvider
from relay_support import key

from camera_relay.adapters.media.pipeline import (
    FakeSourceDriver,
    MediaServer,
    RunningSource,
    Transcoder,
    fake_source_args,
    source_path,
    transcoder_args,
    viewer_path,
)
from camera_relay.adapters.media.process import ProgressStats, SupervisedProcess, redact

RID = "0123456789abcdef0123456789abcdef"
SERVER = MediaServer("rtsp://mediamtx:8554", "relay", lambda: "s3cr/et")


def _value(args: list[str], flag: str) -> str:
    return args[args.index(flag) + 1]


def test_paths_carry_the_session_token_never_the_mower() -> None:
    assert viewer_path(RID, "front_left") == f"r/{RID}/front_left"
    assert source_path(RID, "front_left") == f"r/{RID}/front_left/src"


def test_urls_carry_the_quoted_credential_and_logs_never_do() -> None:
    url = SERVER.url(viewer_path(RID, "front_left"))
    assert url == f"rtsp://relay:s3cr%2Fet@mediamtx:8554/r/{RID}/front_left"
    assert "s3cr" not in redact(f"error on {url}", SERVER.secrets())


def test_the_fake_source_is_h264_baseline_at_10_fps_without_duplicates() -> None:
    mower_id = UUID("1a2b3c4d-0000-4000-8000-000000000000")
    args = fake_source_args(
        ffmpeg="ffmpeg",
        output_url="rtsp://out",
        mower_id=mower_id,
        camera_id="front_left",
        font="/font.ttf",
    )
    source = _value(args, "-i")
    assert source.startswith("testsrc2=size=640x480:rate=10,realtime=limit=0.5,drawtext=")
    assert "1a2b3c4d  front_left" in source
    assert "%{localtime\\:%X.%3N}" in source  # wall clock with milliseconds
    assert "-re" not in args  # realtime paces without a catch-up burst after a freeze
    assert _value(args, "-c:v") == "libx264"
    assert _value(args, "-profile:v") == "baseline"
    assert _value(args, "-bf") == "0"
    assert _value(args, "-g") == "10"
    assert _value(args, "-tune") == "zerolatency"
    assert _value(args, "-fps_mode") == "passthrough"
    assert _value(args, "-progress") == "pipe:1"
    assert "-an" in args
    assert args[-1] == "rtsp://out"


def test_the_overlay_drops_characters_drawtext_would_interpret() -> None:
    args = fake_source_args(
        ffmpeg="ffmpeg", output_url="o", mower_id=uuid4(), camera_id="a:b'c%d", font="/f"
    )
    assert "abcd" in _value(args, "-i")


def test_the_transcoder_reads_the_source_and_never_duplicates_a_frame() -> None:
    args = transcoder_args(ffmpeg="/usr/bin/ffmpeg", input_url="rtsp://in", output_url="rtsp://o")
    assert args[0] == "/usr/bin/ffmpeg"
    assert _value(args, "-i") == "rtsp://in"
    head = args[: args.index("-i")]
    assert _value(head, "-fflags") == "nobuffer"
    assert _value(head, "-flags") == "low_delay"
    tail = args[args.index("-i") :]
    assert _value(tail, "-c:v") == "libx264"
    assert _value(tail, "-preset") == "ultrafast"
    assert _value(tail, "-tune") == "zerolatency"
    assert _value(tail, "-profile:v") == "baseline"
    assert _value(tail, "-bf") == "0"
    assert _value(tail, "-g") == "10"
    assert _value(tail, "-fps_mode") == "passthrough"
    assert "-r" not in args and "-vsync" not in args
    assert "-an" in tail
    assert args[-1] == "rtsp://o"


def test_a_transcoder_maps_src_to_the_viewer_path() -> None:
    transcoder = Transcoder(server=SERVER, ffmpeg="ffmpeg", relay_id=RID, camera_id="uid1")
    args = list(transcoder.process.args)
    assert _value(args, "-i").endswith(f"/r/{RID}/uid1/src")
    assert args[-1].endswith(f"/r/{RID}/uid1")
    assert "s3cr" not in transcoder.process.describe()


def test_progress_lines_update_the_statistics() -> None:
    stats = ProgressStats()
    for line in ("frame=42", "fps=9.9", "dup_frames=0", "drop_frames=3", "speed=1.0x"):
        k, _, v = line.partition("=")
        stats.apply(k, v)
    stats.apply("progress", "continue")
    stats.apply("frame", "not-a-number")
    assert (stats.frames, stats.dup_frames, stats.drop_frames, stats.fps) == (42, 0, 3, 9.9)
    assert stats.updated_at is not None
    assert stats.as_dict()["speed"] == "1.0x"


# --- A fake process for the supervisor -------------------------------------------------------


@dataclass
class FakeProcess:
    pid: int
    progress: list[bytes] = field(default_factory=list)
    returncode: int | None = None
    signals: list[int] = field(default_factory=list)
    stdout: asyncio.StreamReader = field(default_factory=asyncio.StreamReader)
    stderr: asyncio.StreamReader = field(default_factory=asyncio.StreamReader)
    _exited: asyncio.Event = field(default_factory=asyncio.Event)

    def __post_init__(self) -> None:
        for line in self.progress:
            self.stdout.feed_data(line)

    def exit(self, code: int) -> None:
        self.returncode = code
        self.stdout.feed_eof()
        self.stderr.feed_eof()
        self._exited.set()

    async def wait(self) -> int:
        await self._exited.wait()
        assert self.returncode is not None
        return self.returncode

    def terminate(self) -> None:
        self.exit(-15)

    def kill(self) -> None:
        self.exit(-9)

    def send_signal(self, sig: int) -> None:
        self.signals.append(sig)


@dataclass
class FakeSpawn:
    progress: list[bytes] = field(default_factory=list)
    processes: list[FakeProcess] = field(default_factory=list)
    calls: list[tuple[str, ...]] = field(default_factory=list)

    async def __call__(self, *args: str) -> FakeProcess:
        self.calls.append(args)
        process = FakeProcess(pid=100 + len(self.processes), progress=list(self.progress))
        self.processes.append(process)
        return process


async def _settle() -> None:
    for _ in range(5):
        await asyncio.sleep(0)


async def test_the_supervisor_reads_progress_and_stops_cleanly() -> None:
    spawn = FakeSpawn(progress=[b"frame=7\n", b"dup_frames=0\n", b"progress=continue\n"])
    process = SupervisedProcess("p", ["ffmpeg", "-x"], spawn=spawn)
    await process.start()
    await _settle()
    assert process.running and process.pid == 100
    assert process.stats.frames == 7
    await process.stop()
    assert not process.running
    assert spawn.processes[0].returncode == -15
    assert len(spawn.calls) == 1


async def test_an_unexpected_exit_is_restarted_then_fails_for_good() -> None:
    spawn = FakeSpawn()
    failures: list[str] = []
    process = SupervisedProcess(
        "p",
        ["ffmpeg"],
        spawn=spawn,
        on_failure=lambda: failures.append("failed"),
        max_restarts=2,
        restart_delay=0,
    )
    await process.start()
    for _ in range(3):
        await _settle()
        spawn.processes[-1].exit(1)
        await _settle()
    await _settle()
    assert len(spawn.processes) == 3  # first start and two restarts
    assert failures == ["failed"]
    assert process.failed and not process.running


@pytest.mark.skipif(sys.platform == "win32", reason="SIGSTOP/SIGCONT are POSIX signals")
async def test_pause_and_resume_send_sigstop_and_sigcont() -> None:
    spawn = FakeSpawn()
    process = SupervisedProcess("p", ["ffmpeg"], spawn=spawn)
    await process.start()
    assert process.pause() and process.paused
    assert not process.pause()  # already paused
    assert process.resume() and not process.paused
    assert not process.resume()
    assert spawn.processes[0].signals == [signal.SIGSTOP, signal.SIGCONT]
    process.pause()
    await process.stop()  # continued before it is terminated
    assert spawn.processes[0].signals[-1] == signal.SIGCONT


@pytest.mark.skipif(sys.platform != "win32", reason="only Windows lacks SIGSTOP")
async def test_pause_is_not_available_on_windows() -> None:
    spawn = FakeSpawn()
    process = SupervisedProcess("p", ["ffmpeg"], spawn=spawn)
    await process.start()
    with pytest.raises(NotImplementedError):
        process.pause()
    await process.stop()


def _source(cameras: tuple[str, ...] = ("front_left", "front_right")) -> RunningSource:
    return RunningSource(
        key=key(), session_id=uuid4(), relay_id=RID, provider=CameraProvider.FAKE, cameras=cameras
    )


async def test_the_fake_driver_runs_one_process_per_camera() -> None:
    spawn = FakeSpawn()
    driver = FakeSourceDriver(
        server=SERVER, ffmpeg="ffmpeg", font="/f.ttf", on_failure=lambda s: None, spawn=spawn
    )
    source = _source()
    await driver.start(source)
    assert sorted(source.processes) == ["front_left", "front_right"]
    outputs = sorted(call[-1] for call in spawn.calls)
    assert outputs == [SERVER.url(viewer_path(RID, c)) for c in ("front_left", "front_right")]
    await driver.update(source, ["front_left"])
    assert list(source.processes) == ["front_left"]
    assert spawn.processes[1].returncode == -15
    await driver.update(source, ["front_left", "rear"])
    assert sorted(source.processes) == ["front_left", "rear"]
    await driver.stop(source)
    assert source.processes == {}
    assert driver.sources == {}
    assert all(p.returncode is not None for p in spawn.processes)


@pytest.mark.skipif(sys.platform == "win32", reason="SIGSTOP/SIGCONT are POSIX signals")
async def test_freeze_pauses_the_fake_sources_of_one_session_or_all() -> None:
    spawn = FakeSpawn()
    driver = FakeSourceDriver(
        server=SERVER, ffmpeg="ffmpeg", font="/f.ttf", on_failure=lambda s: None, spawn=spawn
    )
    one, other = _source(("front_left",)), _source(("front_left",))
    other.relay_id = "f" * 32
    await driver.start(one)
    await driver.start(other)
    assert driver.freeze(True, RID) == 1
    assert one.processes["front_left"].paused and not other.processes["front_left"].paused
    assert driver.freeze(True) == 1  # only the one still running
    assert driver.freeze(False) == 2
    await driver.stop(one)
    await driver.stop(other)


async def test_a_failing_fake_source_reports_its_source() -> None:
    spawn = FakeSpawn()
    failed: list[RunningSource] = []
    driver = FakeSourceDriver(
        server=SERVER,
        ffmpeg="ffmpeg",
        font="/f.ttf",
        on_failure=failed.append,
        spawn=spawn,
        max_restarts=0,
    )
    source = _source(("front_left",))
    await driver.start(source)
    await _settle()
    spawn.processes[0].exit(1)
    await _settle()
    assert failed == [source]
