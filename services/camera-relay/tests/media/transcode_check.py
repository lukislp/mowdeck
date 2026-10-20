"""Runs inside the relay container of the media test stack (``docker compose exec``).

Replays the synthetic H.265 stream (``camera-h265.h265``, 271 frames, 10 fps) in real
time into ``r/<rid>/uid1/src``, runs the relay's generic :class:`Transcoder` on it and prints
one JSON line: the frames the replay sent, the transcoder's progress (frames written,
duplicated, dropped) and an ffprobe of the H.264 output. The test asserts that the transcoder
never writes more frames than it got and duplicates none.
"""

import asyncio
import json
import os
import secrets
import subprocess
import sys

from camera_relay.adapters.media.pipeline import (
    COMMON,
    MediaServer,
    Transcoder,
    source_path,
    viewer_path,
)
from camera_relay.adapters.media.process import SupervisedProcess

FIXTURE = "/opt/fixtures/camera-h265.h265"
CAMERA = "uid1"


def _probe(url: str) -> dict[str, object]:
    result = subprocess.run(  # noqa: S603
        [  # noqa: S607
            "ffprobe",
            "-v",
            "error",
            "-rtsp_transport",
            "tcp",
            "-show_entries",
            "stream=codec_name,profile,width,height,has_b_frames",
            "-of",
            "json",
            url,
        ],
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    streams = json.loads(result.stdout or "{}").get("streams") or [{}]
    probe: dict[str, object] = streams[0]
    return probe


async def _wait_exit(process: SupervisedProcess, within: float) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + within
    while process.running and loop.time() < deadline:  # noqa: ASYNC110 - polls a supervisor
        await asyncio.sleep(0.5)


async def main() -> None:
    server = MediaServer(
        os.environ["MOWDECK_RELAY_MEDIAMTX_RTSP_URL"],
        os.environ.get("MOWDECK_RELAY_PUBLISHER_USER", "relay"),
        lambda: os.environ["MOWDECK_RELAY_PUBLISHER_SECRET"],
    )
    relay_id = secrets.token_hex(16)
    replay = SupervisedProcess(
        "replay",
        [
            "ffmpeg",
            *COMMON,
            "-re",
            "-f",
            "hevc",
            "-framerate",
            "10",
            "-i",
            FIXTURE,
            "-c",
            "copy",
            "-f",
            "rtsp",
            "-rtsp_transport",
            "tcp",
            server.url(source_path(relay_id, CAMERA)),
        ],
        secrets=server.secrets(),
        max_restarts=0,
    )
    transcoder = Transcoder(
        server=server, ffmpeg="ffmpeg", relay_id=relay_id, camera_id=CAMERA, max_restarts=0
    )
    await replay.start()
    await asyncio.sleep(1.0)
    await transcoder.start()
    await asyncio.sleep(6.0)
    probe = await asyncio.to_thread(_probe, server.url(viewer_path(relay_id, CAMERA)))
    await _wait_exit(replay, 40)
    await _wait_exit(transcoder.process, 20)
    await transcoder.stop()
    await replay.stop()
    print(
        json.dumps(
            {
                "replayed": replay.stats.as_dict(),
                "transcoded": transcoder.process.stats.as_dict(),
                "probe": probe,
            }
        )
    )


if __name__ == "__main__":
    asyncio.run(main())
    sys.exit(0)
