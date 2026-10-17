"""The agora source driver end to end, without Agora: the recorded LUBA stream through the bridge
protocol, the relay's muxer and transcoder, MediaMTX and a WHEP client (ADR 0045 step 7).

The test gateway answers ``agora`` sessions; the stack's agora-bridge server starts
``fake_bridge.py`` per connection, which replays ``luba-uid1.h265`` (H.265) for uid 1 and 2.
Covered: H.264 Constrained Baseline 640x480 at about 10 fps from the H.265 source and the
H.265 ``.../src`` path; a crashing bridge fails the session (``unavailable`` during the
backoff); without a reachable bridge an agora mower is ``unsupported_provider`` and its vendor
session is not released.

Opt-in (Docker): ``MOWDECK_TEST_RELAY_MEDIA=1`` (see relay_media_support.py).
"""

import asyncio
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
from mowdeck_contracts.v1.relay import RelayAttachReply, RelayAttachRequest, RelayErrorCode
from mowdeck_contracts.v1.subjects import (
    ALL_CAMERA_RELEASES,
    ALL_CAMERA_SESSIONS,
    RELAY_CAMERA_ATTACH,
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

    def session_for(self, mower_id: UUID) -> CameraSession:
        prefix = "crash" if mower_id in self.crash_mowers else "luba"
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
