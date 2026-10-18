"""The agora source driver end to end, without Agora: the recorded LUBA stream through the bridge
protocol, the relay's muxer and transcoder, MediaMTX and a WHEP client (ADR 0045 step 7).

The test gateway answers ``agora`` sessions; the stack's agora-bridge server starts
``fake_bridge.py`` per connection, which replays ``luba-uid1.h265`` (H.265) for uid 1 and 2.
Covered: H.264 Constrained Baseline 640x480 at about 10 fps from the H.265 source and the
H.265 ``.../src`` path; a first key frame 4 s after the join followed by a 6 s stall still
plays (the transcoder waits for ``.../src``), the rear camera that never publishes blocks
nothing and is reported unavailable; a link that stalls again and again, a camera that
leaves for good and one that publishes again recover (a new WHEP reader gets H.264 again); a
crashing bridge fails the session (``unavailable`` during the backoff); without a reachable
bridge an agora mower is ``unsupported_provider`` and its vendor session is not released.

Opt-in (Docker): ``MOWDECK_TEST_RELAY_MEDIA=1`` (see relay_media_support.py).
"""

import asyncio
import contextlib
import time
from collections.abc import AsyncIterator
from datetime import timedelta
from uuid import UUID, uuid4

import pytest
from mowdeck_contracts.v1 import (
    CameraReleaseReply,
    CameraReleaseRequest,
    CameraSession,
    CameraSessionReply,
    CameraSessionRequest,
    utc_now,
)
from mowdeck_contracts.v1.camera import CameraInfo, CameraPlacement, CameraProvider
from mowdeck_contracts.v1.relay import (
    RelayAttachReply,
    RelayAttachRequest,
    RelayErrorCode,
    RelayRenewReply,
    RelayRenewRequest,
)
from mowdeck_contracts.v1.subjects import (
    ALL_CAMERA_RELEASES,
    ALL_CAMERA_SESSIONS,
    RELAY_CAMERA_ATTACH,
    RELAY_CAMERA_RENEW,
)
from nats.aio.client import Client
from nats.aio.msg import Msg

from .relay_media_support import MediaStack, eventually, probe
from .test_camera_relay_media import VIEWER, attach, detach, frames_after, watch, whep_url

TOKEN = "agora-media-test-token"  # noqa: S105 - only the fake bridge sees it


class AgoraGateway:
    """Answers camera sessions like the Mammotion adapter: provider agora, three cameras."""

    def __init__(self) -> None:
        self.sessions: list[CameraSessionRequest] = []
        self.releases: list[CameraReleaseRequest] = []
        self.crash_mowers: set[UUID] = set()
        self.late_mowers: set[UUID] = set()
        self.stall_mowers: set[UUID] = set()

    def session_for(self, mower_id: UUID) -> CameraSession:
        prefix = "crash" if mower_id in self.crash_mowers else "luba"
        prefix = "late" if mower_id in self.late_mowers else prefix
        prefix = "stall" if mower_id in self.stall_mowers else prefix
        return CameraSession(
            provider=CameraProvider.AGORA,
            app_id="media-test-app",
            channel=f"{prefix}-{str(mower_id)[:8]}",
            token=TOKEN,
            uid=4000 + len(self.sessions),
            cameras=[
                CameraInfo(id="front_left", name="L", uid=1, placement=CameraPlacement.FRONT_LEFT),
                CameraInfo(
                    id="front_right", name="R", uid=2, placement=CameraPlacement.FRONT_RIGHT
                ),
                CameraInfo(id="rear", name="Rear", uid=3, placement=CameraPlacement.REAR),
            ],
            expires_at=utc_now() + timedelta(hours=1),
        )

    async def on_session(self, msg: Msg) -> None:
        request = CameraSessionRequest.model_validate_json(msg.data)
        self.sessions.append(request)
        reply = CameraSessionReply(session=self.session_for(request.mower_id))
        await msg.respond(reply.model_dump_json().encode())

    async def on_release(self, msg: Msg) -> None:
        self.releases.append(CameraReleaseRequest.model_validate_json(msg.data))
        await msg.respond(CameraReleaseReply(released=True).model_dump_json().encode())


@pytest.fixture
async def agora(bus: Client) -> AsyncIterator[AgoraGateway]:
    fake = AgoraGateway()
    subscriptions = [
        await bus.subscribe(ALL_CAMERA_SESSIONS, cb=fake.on_session),
        await bus.subscribe(ALL_CAMERA_RELEASES, cb=fake.on_release),
    ]
    await bus.flush()
    try:
        yield fake
    finally:
        for subscription in subscriptions:
            await subscription.unsubscribe()


async def attach_reply(bus: Client, account_id: UUID, mower_id: UUID) -> RelayAttachReply:
    request = RelayAttachRequest(
        account_id=account_id,
        mower_id=mower_id,
        camera_ids=[],
        requested_by=VIEWER,
    )
    message = await bus.request(RELAY_CAMERA_ATTACH, request.model_dump_json().encode(), timeout=15)
    return RelayAttachReply.model_validate_json(message.data)


async def test_an_agora_mower_plays_through_the_bridge_as_h264(
    media_stack: MediaStack, bus: Client, agora: AgoraGateway
) -> None:
    account_id, mower_id = uuid4(), uuid4()
    session = await attach(bus, account_id, mower_id, ["front_left", "front_right"])
    assert session.relay is not None
    assert session.token == "" and session.app_id == ""  # no vendor secret reaches a viewer
    viewer = await watch(whep_url(media_stack, session), session.relay.ticket, within=40)
    try:
        assert await frames_after(viewer, 5, 20), "no frame decoded"
        start = time.monotonic()
        await asyncio.sleep(4)
        fps = viewer.count_since(start) / (time.monotonic() - start)
        assert 7 <= fps <= 13, fps
        assert viewer.sizes == {(640, 480)}
        path = whep_url(media_stack, session).removeprefix(media_stack.whep_url + "/")
        path = path.removesuffix("/whep")
        out = probe(media_stack, path)
        assert out.get("codec_name") == "h264", out
        assert out.get("profile") == "Constrained Baseline", out
        assert out.get("has_b_frames") == 0
        source = probe(media_stack, f"{path}/src")
        assert source.get("codec_name") == "hevc", source
        assert (source.get("width"), source.get("height")) == (640, 480)
        second = await watch(whep_url(media_stack, session, 1), session.relay.ticket, within=40)
        try:
            assert await frames_after(second, 5, 20)
        finally:
            await second.close()
    finally:
        await viewer.close()
        await detach(bus, account_id, mower_id, session.relay.lease_id)


async def test_a_late_first_key_frame_and_a_silent_camera_still_play(
    media_stack: MediaStack, bus: Client, agora: AgoraGateway
) -> None:
    # Live incident: the transcoder read .../src before the muxer had published it (the LUBA
    # sends a key frame every 4 s), got 404 four times and failed the whole session.
    account_id, mower_id = uuid4(), uuid4()
    agora.late_mowers.add(mower_id)
    session = await attach(bus, account_id, mower_id, [])  # every listed camera, rear too
    assert session.relay is not None
    assert [t.camera_id for t in session.relay.tiles] == ["front_left", "front_right", "rear"]
    viewer = await watch(whep_url(media_stack, session), session.relay.ticket, within=40)
    try:
        assert await frames_after(viewer, 10, 20), "no frame decoded"
        assert viewer.sizes == {(640, 480)}
        second = await watch(whep_url(media_stack, session, 1), session.relay.ticket, within=30)
        try:
            assert await frames_after(second, 5, 20)
        finally:
            await second.close()

        async def rear_unavailable() -> bool:
            reply = await renew(bus, account_id, mower_id, session)
            assert reply.session is not None and reply.session.relay is not None, reply.error
            tiles = {t.camera_id: t.available for t in reply.session.relay.tiles}
            return tiles == {"front_left": True, "front_right": True, "rear": False}

        assert await eventually(rear_unavailable, 40), "the rear camera is still offered"
    finally:
        await viewer.close()
        await detach(bus, account_id, mower_id, session.relay.lease_id)


async def renew(
    bus: Client, account_id: UUID, mower_id: UUID, session: CameraSession
) -> RelayRenewReply:
    assert session.relay is not None
    request = RelayRenewRequest(
        account_id=account_id,
        mower_id=mower_id,
        lease_id=session.relay.lease_id,
        requested_by=VIEWER,
    )
    message = await bus.request(RELAY_CAMERA_RENEW, request.model_dump_json().encode(), timeout=10)
    return RelayRenewReply.model_validate_json(message.data)


async def test_a_flapping_link_and_a_republished_camera_recover(
    media_stack: MediaStack, bus: Client, agora: AgoraGateway
) -> None:
    # Live incident 2026-10-18: the LUBA's 4G link stalled again and again while it mowed,
    # MediaMTX dropped the publishers, Agora reported both cameras unpublished and only one of
    # them published again. The relay never started that camera's muxer again: every WHEP
    # offer got 404 while the web player kept the session alive with new leases.
    account_id, mower_id = uuid4(), uuid4()
    agora.stall_mowers.add(mower_id)
    session = await attach(bus, account_id, mower_id, ["front_left", "front_right"])
    assert session.relay is not None
    began = time.monotonic()
    viewer = await watch(whep_url(media_stack, session), session.relay.ticket, within=40)
    leases = [session.relay.lease_id]
    try:
        assert await frames_after(viewer, 5, 20), "no frame before the stall"
        await viewer.close()
        # Like the web player that gets 404: a new lease about every 20 s keeps the session.
        latest = session
        while time.monotonic() - began < REPUBLISHED_AFTER:
            await asyncio.sleep(15)
            latest = await attach(bus, account_id, mower_id, ["front_left", "front_right"])
            assert latest.relay is not None
            leases.append(latest.relay.lease_id)
        assert latest.relay is not None
        # front_left (uid 1) publishes again: offer (404 while it recovers) until it plays.
        viewer = await watch(whep_url(media_stack, latest), latest.relay.ticket, within=25)
        assert await frames_after(viewer, 10, 20), "no frame after the recovery"
        assert viewer.sizes == {(640, 480)}
        path = whep_url(media_stack, latest).removeprefix(media_stack.whep_url + "/")
        out = probe(media_stack, path.removesuffix("/whep"))
        assert out.get("codec_name") == "h264", out
    finally:
        await viewer.close()
        for lease in leases:
            with contextlib.suppress(Exception):
                await detach(bus, account_id, mower_id, lease)


REPUBLISHED_AFTER = 75.0
"""When the fake bridge's "stall" channel has published uid 1 again (fake_bridge.py: 8 s of
frames, three stalls of 12 s with 5 s of frames between, a last stall of 12 s, 2 s unpublished),
counted from the attach with some margin."""


async def test_a_crashing_bridge_fails_the_session_into_the_backoff(
    media_stack: MediaStack, bus: Client, agora: AgoraGateway
) -> None:
    account_id, mower_id = uuid4(), uuid4()
    agora.crash_mowers.add(mower_id)
    session = await attach(bus, account_id, mower_id, ["front_left"])
    assert session.relay is not None
    viewer = await watch(whep_url(media_stack, session), session.relay.ticket, within=40)
    try:
        assert await frames_after(viewer, 3, 20)
        # The fake bridge exits with 1 after 15 s: the source fails, the lease closes.

        async def ended() -> bool:
            return viewer.ended.is_set()

        assert await eventually(ended, 30), "the viewer kept playing a failed source"
        reply = await attach_reply(bus, account_id, mower_id)
        assert reply.error is not None and reply.error.code is RelayErrorCode.UNAVAILABLE
    finally:
        await viewer.close()


async def test_without_a_reachable_bridge_agora_is_unsupported_and_not_released(
    media_stack: MediaStack, bus: Client, agora: AgoraGateway
) -> None:
    await asyncio.to_thread(media_stack.compose, "stop", "agora-bridge")
    try:
        account_id, mower_id = uuid4(), uuid4()
        reply = await attach_reply(bus, account_id, mower_id)
        assert reply.error is not None
        assert reply.error.code is RelayErrorCode.UNSUPPORTED_PROVIDER, reply.error
        await asyncio.sleep(1)
        assert not [r for r in agora.releases if r.mower_id == mower_id]
    finally:
        await asyncio.to_thread(
            media_stack.compose, "up", "-d", "--wait", "--wait-timeout", "60", "agora-bridge"
        )
