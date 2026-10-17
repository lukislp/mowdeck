"""The media plane end to end: real MediaMTX, the relay image, aiortc as the WHEP client.

A test answers the gateway's camera requests (provider ``fake``), attaches over NATS like
Fleet will, reads the WHEP endpoint with the lease's ticket (MediaMTX's WebRTC server directly:
Traefik only strips ``/media/whep``) and decodes the frames. Covered: H.264 Constrained Baseline
640x480 at about 10 fps; no ticket or a foreign ticket is 401; the answer negotiates NACK, PLI
and transport-cc; the freeze hook stalls the frames and resuming continues without a burst;
detach kicks the viewer; after the last lease the 15 s grace ends the fake source and the
stream is released; the transcoder replays the spike's recorded H.265 stream frame for frame.

Opt-in (Docker): ``MOWDECK_TEST_RELAY_MEDIA=1`` (see relay_media_support.py).
"""

import asyncio
import contextlib
import json
import re
import time
from dataclasses import dataclass, field
from uuid import UUID, uuid4

import httpx
from aiortc import RTCConfiguration, RTCPeerConnection, RTCSessionDescription
from aiortc.mediastreams import MediaStreamError, MediaStreamTrack
from mowdeck_contracts.v1 import PrincipalKind
from mowdeck_contracts.v1.camera import CameraSession
from mowdeck_contracts.v1.principal import PrincipalRef
from mowdeck_contracts.v1.relay import (
    RelayAttachReply,
    RelayAttachRequest,
    RelayDetachReply,
    RelayDetachRequest,
    RelayPurpose,
)
from mowdeck_contracts.v1.subjects import RELAY_CAMERA_ATTACH, RELAY_CAMERA_DETACH
from nats.aio.client import Client

from .relay_media_support import FakeGateway, MediaStack, api_get, eventually, probe

WHEP_PREFIX = "/media/whep"


@dataclass(slots=True)
class Viewer:
    """A WHEP reader that decodes and counts frames."""

    pc: RTCPeerConnection
    answer: str
    location: str
    frames: list[float] = field(default_factory=list)
    sizes: set[tuple[int, int]] = field(default_factory=set)
    ended: asyncio.Event = field(default_factory=asyncio.Event)
    task: asyncio.Task[None] | None = None

    def count_since(self, since: float) -> int:
        return sum(1 for t in self.frames if t >= since)

    async def close(self) -> None:
        if self.task is not None:
            self.task.cancel()
            with contextlib.suppress(asyncio.CancelledError, MediaStreamError):
                await self.task
        await self.pc.close()


VIEWER = PrincipalRef(subject=uuid4(), kind=PrincipalKind.USER)
"""The person of every viewer lease here (renew and detach must come from the opener)."""


async def attach(
    bus: Client, account_id: UUID, mower_id: UUID, cameras: list[str]
) -> CameraSession:
    request = RelayAttachRequest(
        account_id=account_id,
        mower_id=mower_id,
        camera_ids=cameras,
        requested_by=VIEWER,
        purpose=RelayPurpose.VIEWER,
    )
    message = await bus.request(RELAY_CAMERA_ATTACH, request.model_dump_json().encode(), timeout=15)
    reply = RelayAttachReply.model_validate_json(message.data)
    assert reply.error is None, reply.error
    assert reply.session is not None and reply.session.relay is not None
    return reply.session


async def detach(bus: Client, account_id: UUID, mower_id: UUID, lease_id: UUID) -> bool:
    request = RelayDetachRequest(
        account_id=account_id,
        mower_id=mower_id,
        lease_id=lease_id,
        requested_by=VIEWER,
    )
    message = await bus.request(RELAY_CAMERA_DETACH, request.model_dump_json().encode(), 10)
    return RelayDetachReply.model_validate_json(message.data).detached


def whep_url(stack: MediaStack, session: CameraSession, index: int = 0) -> str:
    assert session.relay is not None
    tile = session.relay.tiles[index].whep_url
    assert tile.startswith(f"{WHEP_PREFIX}/r/"), tile
    return stack.whep_url + tile.removeprefix(WHEP_PREFIX)


async def post_offer(url: str, sdp: str, ticket: str | None) -> httpx.Response:
    headers = {"Content-Type": "application/sdp"}
    if ticket is not None:
        headers["Authorization"] = f"Bearer {ticket}"
    async with httpx.AsyncClient(timeout=10) as client:
        return await client.post(url, content=sdp, headers=headers)


async def offer() -> RTCPeerConnection:
    # No STUN server: the relay's ICE host candidates are all a client needs in a LAN.
    pc = RTCPeerConnection(RTCConfiguration(iceServers=[]))
    pc.addTransceiver("video", direction="recvonly")
    await pc.setLocalDescription(await pc.createOffer())
    while pc.iceGatheringState != "complete":  # noqa: ASYNC110 - aiortc has no gathering event
        await asyncio.sleep(0.05)
    return pc


async def _open(url: str, ticket: str, deadline: float) -> tuple[RTCPeerConnection, httpx.Response]:
    """POST an offer, retrying while the source is not ready yet (404)."""
    loop = asyncio.get_running_loop()
    while True:
        pc = await offer()
        response = await post_offer(url, pc.localDescription.sdp, ticket)
        if response.status_code == 201:
            return pc, response
        await pc.close()
        assert response.status_code in (400, 404, 500), (response.status_code, response.text)
        assert loop.time() < deadline, f"no stream: {response.status_code} {response.text}"
        await asyncio.sleep(0.5)


async def watch(url: str, ticket: str, *, within: float = 30) -> Viewer:
    """Open a WHEP reader that decodes frames. Like a browser it reconnects when ICE does not
    connect within a few seconds (Docker Desktop's port forwarding drops a first UDP flow at
    times)."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + within
    while True:
        pc, response = await _open(url, ticket, deadline)
        viewer = Viewer(pc=pc, answer=response.text, location=response.headers["location"])
        connected = asyncio.Event()

        @pc.on("track")
        def on_track(track: MediaStreamTrack, viewer: Viewer = viewer) -> None:
            async def consume() -> None:
                try:
                    while True:
                        frame = await track.recv()
                        viewer.frames.append(time.monotonic())
                        viewer.sizes.add((frame.width, frame.height))  # type: ignore[union-attr]
                except MediaStreamError:
                    viewer.ended.set()

            viewer.task = asyncio.ensure_future(consume())

        @pc.on("connectionstatechange")
        def on_state(
            pc: RTCPeerConnection = pc,
            viewer: Viewer = viewer,
            connected: asyncio.Event = connected,
        ) -> None:
            if pc.connectionState == "connected":
                connected.set()
            if pc.connectionState in ("failed", "closed"):
                viewer.ended.set()

        await pc.setRemoteDescription(RTCSessionDescription(sdp=response.text, type="answer"))
        try:
            await asyncio.wait_for(connected.wait(), 6)
            return viewer
        except TimeoutError:
            await viewer.close()
            async with httpx.AsyncClient() as client:
                await client.delete(url.split("/r/")[0] + viewer.location)
            assert loop.time() < deadline, "ICE did not connect"


async def frames_after(viewer: Viewer, count: int, within: float) -> bool:
    async def enough() -> bool:
        return len(viewer.frames) >= count

    return bool(await eventually(enough, within, 0.2))


async def test_attach_plays_h264_constrained_baseline_at_10_fps(
    media_stack: MediaStack, bus: Client, gateway: FakeGateway
) -> None:
    account_id, mower_id = uuid4(), uuid4()
    session = await attach(bus, account_id, mower_id, ["front_left", "front_right"])
    assert session.relay is not None
    assert [s.account_id for s in gateway.sessions] == [account_id]
    assert gateway.sessions[0].requested_by.kind is PrincipalKind.SERVICE
    viewer = await watch(whep_url(media_stack, session), session.relay.ticket)
    try:
        assert await frames_after(viewer, 5, 15), "no frame decoded"
        start = time.monotonic()
        await asyncio.sleep(4)
        fps = viewer.count_since(start) / (time.monotonic() - start)
        assert 7 <= fps <= 13, fps
        assert viewer.sizes == {(640, 480)}
        assert re.search(r"a=rtpmap:\d+ H264/90000", viewer.answer)
        assert "profile-level-id=42e0" in viewer.answer.lower()
        path = whep_url(media_stack, session).removeprefix(media_stack.whep_url + "/")
        stream = probe(media_stack, path.removesuffix("/whep"))
        assert stream.get("codec_name") == "h264", stream
        assert stream.get("profile") == "Constrained Baseline", stream
        assert (stream.get("width"), stream.get("height")) == (640, 480)
        assert stream.get("has_b_frames") == 0
        # The second camera has its own stream on its own path.
        second = await watch(whep_url(media_stack, session, 1), session.relay.ticket)
        try:
            assert await frames_after(second, 5, 15)
        finally:
            await second.close()
    finally:
        await viewer.close()
        await detach(bus, account_id, mower_id, session.relay.lease_id)


async def test_readers_need_a_ticket_of_their_own_lease(
    media_stack: MediaStack, bus: Client, gateway: FakeGateway
) -> None:
    del gateway
    account_a, account_b, mower_id = uuid4(), uuid4(), uuid4()
    session_a = await attach(bus, account_a, mower_id, ["front_left"])
    session_b = await attach(bus, account_b, mower_id, ["front_left"])
    assert session_a.relay is not None and session_b.relay is not None
    url_a = whep_url(media_stack, session_a)
    try:
        # Wait until A's stream is ready, so a 401 is about the ticket, not the stream.
        viewer = await watch(url_a, session_a.relay.ticket)
        await viewer.close()
        pc = await offer()
        try:
            sdp = pc.localDescription.sdp
            assert (await post_offer(url_a, sdp, None)).status_code == 401
            assert (await post_offer(url_a, sdp, session_b.relay.ticket)).status_code == 401
            tampered = session_a.relay.ticket[:-4] + "AAAA"
            assert (await post_offer(url_a, sdp, tampered)).status_code == 401
            assert (await post_offer(url_a, sdp, "not-a-ticket")).status_code == 401
            # The same mower under account B is another session with another path.
            assert whep_url(media_stack, session_b) != url_a
        finally:
            await pc.close()
    finally:
        await detach(bus, account_a, mower_id, session_a.relay.lease_id)
        await detach(bus, account_b, mower_id, session_b.relay.lease_id)


async def test_the_answer_negotiates_nack_pli_and_transport_cc(
    media_stack: MediaStack, bus: Client, gateway: FakeGateway
) -> None:
    del gateway
    account_id, mower_id = uuid4(), uuid4()
    session = await attach(bus, account_id, mower_id, ["front_left"])
    assert session.relay is not None
    url = whep_url(media_stack, session)
    try:
        viewer = await watch(url, session.relay.ticket)
        await viewer.close()
        pc = await offer()
        # aiortc offers goog-remb; browsers offer transport-cc with its header extension.
        sdp = re.sub(
            r"(a=rtcp-fb:(\d+) goog-remb\r\n)",
            lambda m: f"{m.group(1)}a=rtcp-fb:{m.group(2)} transport-cc\r\n",
            pc.localDescription.sdp,
        ).replace(
            "a=extmap:3 http://www.webrtc.org/experiments/rtp-hdrext/abs-send-time\r\n",
            "a=extmap:3 http://www.webrtc.org/experiments/rtp-hdrext/abs-send-time\r\n"
            "a=extmap:5 http://www.ietf.org/id/draft-holmer-rmcat-transport-wide-cc-extensions-01"
            "\r\n",
        )
        await pc.close()
        response = await post_offer(url, sdp, session.relay.ticket)
        assert response.status_code == 201, response.text
        answer = response.text
        payload = re.search(r"a=rtpmap:(\d+) H264/90000", answer)
        assert payload is not None
        pt = payload.group(1)
        for feedback in ("nack", "nack pli", "transport-cc"):
            assert f"a=rtcp-fb:{pt} {feedback}\r\n" in answer, feedback
        async with httpx.AsyncClient() as client:
            await client.delete(media_stack.whep_url + response.headers["location"])
    finally:
        await detach(bus, account_id, mower_id, session.relay.lease_id)


async def test_a_frozen_source_sends_no_frames_and_resumes_without_a_burst(
    media_stack: MediaStack, bus: Client, gateway: FakeGateway
) -> None:
    del gateway
    account_id, mower_id = uuid4(), uuid4()
    session = await attach(bus, account_id, mower_id, ["front_left"])
    assert session.relay is not None
    viewer = await watch(whep_url(media_stack, session), session.relay.ticket)
    relay_id = session.relay.tiles[0].whep_url.split("/")[4]
    try:
        assert await frames_after(viewer, 10, 15)
        async with httpx.AsyncClient(timeout=5) as client:
            freeze = await client.post(
                f"{media_stack.relay_url}/internal/v1/test/freeze",
                json={"frozen": True, "relay_id": relay_id},
            )
            assert freeze.json() == {"frozen": True, "changed": 1}
            await asyncio.sleep(0.5)  # frames in flight
            frozen_at = time.monotonic()
            await asyncio.sleep(3)
            assert viewer.count_since(frozen_at) <= 1, "frames arrived from a frozen source"
            resume = await client.post(
                f"{media_stack.relay_url}/internal/v1/test/freeze",
                json={"frozen": False, "relay_id": relay_id},
            )
            assert resume.json() == {"frozen": False, "changed": 1}
        resumed_at = time.monotonic()
        assert await frames_after(viewer, len(viewer.frames) + 5, 10), "no frames after resume"
        await asyncio.sleep(2)
        # 10 fps after the pause: no catch-up burst of the frozen seconds.
        first_second = sum(1 for t in viewer.frames if resumed_at <= t < resumed_at + 1.5)
        assert first_second <= 20, first_second
        assert not viewer.ended.is_set(), "a short freeze must not end the WHEP session"
    finally:
        await viewer.close()
        await detach(bus, account_id, mower_id, session.relay.lease_id)


async def test_detach_kicks_the_viewer_and_the_grace_ends_and_releases_the_source(
    media_stack: MediaStack, bus: Client, gateway: FakeGateway
) -> None:
    account_id, mower_id = uuid4(), uuid4()
    session = await attach(bus, account_id, mower_id, ["front_left"])
    assert session.relay is not None
    viewer = await watch(whep_url(media_stack, session), session.relay.ticket)
    path = whep_url(media_stack, session).removeprefix(media_stack.whep_url + "/")
    path = path.removesuffix("/whep")

    async def readers() -> list[str]:
        body = await api_get(media_stack, "/v3/webrtcsessions/list")
        return [item["id"] for item in body["items"] if item["path"] == path]

    try:
        assert await frames_after(viewer, 10, 15)
        # The reconcile loop (2 s) confirms the media; only a confirmed viewer is kicked.
        await asyncio.sleep(5)
        assert await readers()
        assert await detach(bus, account_id, mower_id, session.relay.lease_id)

        async def kicked() -> bool:
            return not await readers()

        assert await eventually(kicked, 10), "the detached viewer still reads"
        await asyncio.wait_for(viewer.ended.wait(), 15)
    finally:
        await viewer.close()
    detached_at = time.monotonic()

    async def source_gone() -> bool:
        body = await api_get(media_stack, "/v3/paths/list")
        return not any(item["name"] == path and item["ready"] for item in body["items"])

    await asyncio.sleep(10)
    assert not await source_gone(), "the source stopped before the grace ended"
    assert await eventually(source_gone, 15), "the fake source still publishes after the grace"
    assert time.monotonic() - detached_at >= 14

    async def released() -> bool:
        return any(r.mower_id == mower_id for r in gateway.releases)

    assert await eventually(released, 10), "no camera release after the grace"
    release = next(r for r in gateway.releases if r.mower_id == mower_id)
    assert release.account_id == account_id
    assert release.channel == f"sim-{str(mower_id)[:8]}"
    assert release.requested_by.kind is PrincipalKind.SERVICE


async def test_the_transcoder_keeps_the_frame_count_of_the_recorded_stream(
    media_stack: MediaStack,
) -> None:
    result = await asyncio.to_thread(
        media_stack.compose,
        "exec",
        "-T",
        "camera-relay",
        "python",
        "/opt/media-tests/transcode_check.py",
        check=False,
    )
    assert result.returncode == 0, result.stderr[-3000:]
    report = json.loads(result.stdout.strip().splitlines()[-1])
    replayed, transcoded = report["replayed"], report["transcoded"]
    assert replayed["frames"] == 271, replayed
    assert transcoded["dup_frames"] == 0, transcoded
    # Joined about a second in and decoding starts at the next key frame (4 s GOP): never more
    # frames than came in, and every frame after the first key frame.
    assert 271 - 60 <= transcoded["frames"] <= replayed["frames"], report
    probe_result = report["probe"]
    assert probe_result.get("codec_name") == "h264", probe_result
    assert probe_result.get("profile") == "Constrained Baseline", probe_result
    assert (probe_result.get("width"), probe_result.get("height")) == (640, 480)
