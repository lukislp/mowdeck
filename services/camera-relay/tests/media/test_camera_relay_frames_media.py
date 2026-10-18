"""Vision's frame tap end to end (ADR 0045): the relay image, MediaMTX, ffmpeg and Pillow.

The test publishes the mower's state on ``telemetry.mower.<id>`` (the relay never asks a mower)
and calls the frame tap like the Device-Gateway. Covered with the ``fake`` source: a snapshot is
a valid 640x480 JPEG within its size cap, opens a vision lease for the request only, and the
vision-only session stops after the 15 s grace and is released; a stream yields JPEG parts at
the requested rate and ends its lease when the client goes away; a low battery is refused
(409) without asking the gateway. With the recorded LUBA stream through the fake agora bridge
(H.265 transcoded to H.264): a snapshot is a valid 640x480 JPEG.

Opt-in (Docker): ``MOWDECK_TEST_RELAY_MEDIA=1`` (see relay_media_support.py).
"""

import asyncio
import io
import time
from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

import httpx
from mowdeck_contracts.v1 import ConnectionKind, MowerActivity, MowerState, utc_now
from mowdeck_contracts.v1.subjects import telemetry_subject
from nats.aio.client import Client
from PIL import Image

from .relay_media_support import FakeGateway, MediaStack, eventually
from .test_camera_relay_agora_media import AgoraGateway, agora

__all__ = ["agora"]

FRAMES_TOKEN = "media-test-frames-token"  # noqa: S105 - the test stack's fixed token
AUTH = {"Authorization": f"Bearer {FRAMES_TOKEN}"}


async def publish_state(bus: Client, account_id: UUID, mower_id: UUID, **changes: Any) -> None:
    """The mower's state as the Device-Gateway publishes it."""
    now = utc_now()
    values: dict[str, Any] = {
        "mower_id": mower_id,
        "account_id": account_id,
        "observed_at": now,
        "heartbeat_at": now,
        "activity": MowerActivity.DOCKED,
        "battery_percent": 80,
        "connection": ConnectionKind.CLOUD,
    }
    values.update(changes)
    await bus.publish(telemetry_subject(mower_id), MowerState(**values).model_dump_json().encode())
    await bus.flush()
    await asyncio.sleep(0.5)  # the relay's consumer gets it


async def snapshot(
    stack: MediaStack, account_id: UUID, mower_id: UUID, **body: Any
) -> httpx.Response:
    async with httpx.AsyncClient(timeout=40) as client:
        return await client.post(
            f"{stack.relay_url}/internal/v1/frames/snapshot",
            json={
                "account_id": str(account_id),
                "mower_id": str(mower_id),
                "max_width": 1280,
                "max_bytes": 200_000,
                "timeout_s": 25,
                **body,
            },
            headers=AUTH,
        )


def decoded(data: bytes) -> tuple[str, tuple[int, int]]:
    with Image.open(io.BytesIO(data)) as image:
        image.load()
        return image.format or "", image.size


async def test_a_snapshot_is_a_640x480_jpeg_and_the_vision_session_ends_after_the_grace(
    media_stack: MediaStack, bus: Client, gateway: FakeGateway
) -> None:
    account_id, mower_id = uuid4(), uuid4()
    await publish_state(bus, account_id, mower_id)
    started = time.monotonic()
    response = await snapshot(media_stack, account_id, mower_id)
    assert response.status_code == 200, response.text
    assert time.monotonic() - started < 20
    assert response.headers["content-type"] == "image/jpeg"
    assert len(response.content) <= 200_000
    assert decoded(response.content) == ("JPEG", (640, 480))
    received = datetime.fromisoformat(response.headers["x-frame-received-at"])
    assert abs((utc_now() - received).total_seconds()) < 30
    assert response.headers["x-frame-width"] == "640"
    assert [s.mower_id for s in gateway.sessions] == [mower_id]
    done = time.monotonic()

    async def released() -> bool:
        return any(r.mower_id == mower_id for r in gateway.releases)

    await asyncio.sleep(8)
    assert not await released(), "the vision-only session stopped before the grace ended"
    assert await eventually(released, 20), "no camera release after the vision request"
    assert time.monotonic() - done >= 14
    release = next(r for r in gateway.releases if r.mower_id == mower_id)
    assert release.account_id == account_id


async def test_a_small_size_cap_lowers_the_quality_or_refuses(
    media_stack: MediaStack, bus: Client, gateway: FakeGateway
) -> None:
    del gateway
    account_id, mower_id = uuid4(), uuid4()
    await publish_state(bus, account_id, mower_id)
    small = await snapshot(media_stack, account_id, mower_id, max_width=320, max_bytes=30_000)
    assert small.status_code == 200, small.text
    assert len(small.content) <= 30_000
    assert decoded(small.content) == ("JPEG", (320, 240))
    tiny = await snapshot(media_stack, account_id, mower_id, max_bytes=1024)
    assert tiny.status_code == 502
    assert tiny.json()["type"].endswith(":frame-failed")


async def test_a_low_battery_refuses_vision_without_asking_the_gateway(
    media_stack: MediaStack, bus: Client, gateway: FakeGateway
) -> None:
    account_id, mower_id = uuid4(), uuid4()
    await publish_state(bus, account_id, mower_id, battery_percent=9)
    response = await snapshot(media_stack, account_id, mower_id)
    assert response.status_code == 409
    assert response.json()["type"].endswith(":camera-budget")
    asleep = uuid4()
    await publish_state(bus, account_id, asleep, activity=MowerActivity.OFFLINE)
    offline = await snapshot(media_stack, account_id, asleep)
    assert offline.status_code == 503
    assert not [s for s in gateway.sessions if s.mower_id in (mower_id, asleep)]


async def test_a_stream_yields_parts_at_the_requested_rate_and_ends_with_the_client(
    media_stack: MediaStack, bus: Client, gateway: FakeGateway
) -> None:
    account_id, mower_id = uuid4(), uuid4()
    await publish_state(bus, account_id, mower_id)
    arrivals: list[float] = []
    sizes: set[tuple[int, int]] = set()
    params: dict[str, str | int] = {
        "account_id": str(account_id),
        "mower_id": str(mower_id),
        "fps": 2,
        "max_width": 640,
    }
    async with (
        httpx.AsyncClient(timeout=40) as client,
        client.stream(
            "GET", f"{media_stack.relay_url}/internal/v1/frames/stream", params=params, headers=AUTH
        ) as response,
    ):
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("multipart/x-mixed-replace")
        buffer = b""
        async for chunk in response.aiter_bytes():
            buffer += chunk
            while True:
                head_end = buffer.find(b"\r\n\r\n")
                if head_end < 0:
                    break
                head = buffer[:head_end].decode("ascii", "replace")
                length = int(
                    next(
                        line.split(": ", 1)[1]
                        for line in head.split("\r\n")
                        if line.lower().startswith("content-length")
                    )
                )
                if len(buffer) < head_end + 4 + length + 2:
                    break
                part = buffer[head_end + 4 : head_end + 4 + length]
                buffer = buffer[head_end + 4 + length + 2 :]
                assert "X-Frame-Received-At" in head
                arrivals.append(time.monotonic())
                sizes.add(decoded(part)[1])
            if len(arrivals) >= 8:
                break
    closed_at = time.monotonic()
    assert sizes == {(640, 480)}
    span = arrivals[-1] - arrivals[1]  # from the second part: the first waits for a key frame
    rate = (len(arrivals) - 2) / span
    assert 1.4 <= rate <= 2.6, f"{rate:.2f} parts per second at fps=2"

    async def released() -> bool:
        return any(r.mower_id == mower_id for r in gateway.releases)

    assert await eventually(released, 30), "the stream's lease was not ended with the client"
    assert time.monotonic() - closed_at >= 14


async def test_a_snapshot_of_the_recorded_luba_stream_through_the_agora_bridge(
    media_stack: MediaStack, bus: Client, agora: AgoraGateway
) -> None:
    account_id, mower_id = uuid4(), uuid4()
    await publish_state(bus, account_id, mower_id)
    response = await snapshot(media_stack, account_id, mower_id)
    assert response.status_code == 200, response.text
    assert decoded(response.content) == ("JPEG", (640, 480))
    assert len(response.content) <= 200_000
    assert [s.mower_id for s in agora.sessions] == [mower_id]

    async def released() -> bool:
        return any(r.mower_id == mower_id for r in agora.releases)

    assert await eventually(released, 30), "the vision-only agora session was not released"
