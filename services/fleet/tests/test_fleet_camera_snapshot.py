"""``GET /mowers/{id}/camera/snapshot``: a still image through the Device-Gateway's photo
request, the minimum interval per mower, the checks, the error mapping and the audit."""

import asyncio
import base64
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from uuid import UUID

import pytest
from fleet_support import API, harness
from mowdeck_authz import mower
from mowdeck_contracts.v1 import (
    PhotoError,
    PhotoErrorCode,
    PhotoFrame,
    PhotoReply,
    PhotoRequest,
    VisionDemand,
)
from mowdeck_contracts.v1.subjects import ALL_PHOTO_REQUESTS
from mowdeck_service_kit.bus import BusMessage, InMemoryBus
from mowdeck_service_kit.testing import FakeEntitlements

from fleet.application.camera import CAMERA_SNAPSHOT, CameraService
from fleet.domain.errors import Unavailable

JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 32 + b"\xff\xd9"
TAKEN = datetime(2026, 10, 22, 9, 30, tzinfo=UTC)


class MovableClock:
    """A clock the test moves forward."""

    def __init__(self) -> None:
        self.now = datetime(2026, 10, 22, 12, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, delta: timedelta) -> None:
        self.now += delta


class FakePhotoGateway:
    """Answers photo requests like the Device-Gateway; ``reply`` None stays silent."""

    def __init__(self) -> None:
        self.reply: PhotoReply | None = PhotoReply(
            image_base64=base64.b64encode(JPEG).decode(), media_type="image/jpeg", taken_at=TAKEN
        )
        self.requests: list[PhotoRequest] = []
        self.delay = 0.0

    async def handle(self, message: BusMessage) -> None:
        self.requests.append(message.model(PhotoRequest))
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.reply is not None:
            await message.respond(self.reply)


async def _gateway(bus: InMemoryBus) -> FakePhotoGateway:
    fake = FakePhotoGateway()
    await bus.subscribe(ALL_PHOTO_REQUESTS, fake.handle)
    return fake


def _url(mower_id: str | UUID) -> str:
    return f"{API}/mowers/{mower_id}/camera/snapshot"


async def test_fleet_snapshot_is_taken_through_the_gateway_and_audited() -> None:
    bus = InMemoryBus()
    clock = MovableClock()
    async with harness(bus=bus, clock=clock) as h:
        fake = await _gateway(bus)
        owner = await h.owner()
        viewer = await h.member(owner, "viewer")
        created = await h.register(owner)
        response = await h.client.get(_url(created["id"]), headers=viewer.headers)
        assert response.status_code == 200, response.text
        assert response.content == JPEG
        assert response.headers["content-type"] == "image/jpeg"
        assert response.headers["cache-control"] == "private, no-store"
        assert response.headers["x-snapshot-taken-at"] == TAKEN.isoformat()
        (request,) = fake.requests
        assert str(request.mower_id) == created["id"]
        assert request.account_id == owner.account_id
        assert request.demand is VisionDemand.ROUTINE
        assert request.max_frames == 1
        async with h.store.transaction() as tx:
            (row,) = await tx.audit.find(action=CAMERA_SNAPSHOT)
        assert row.target_id == created["id"]
        assert row.actor_subject == f"user:{viewer.principal.subject}"
        assert row.details == {"media_type": "image/jpeg", "bytes": len(JPEG)}


async def test_fleet_snapshot_keeps_the_minimum_interval_per_mower() -> None:
    bus = InMemoryBus()
    clock = MovableClock()
    async with harness(bus=bus, clock=clock, camera_snapshot_min_interval_seconds=30) as h:
        fake = await _gateway(bus)
        owner = await h.owner()
        first = await h.register(owner)
        second = await h.register(owner)
        for _ in range(3):
            assert (await h.client.get(_url(first["id"]), headers=owner.headers)).status_code == 200
        assert len(fake.requests) == 1
        # Another mower has its own interval.
        assert (await h.client.get(_url(second["id"]), headers=owner.headers)).status_code == 200
        assert len(fake.requests) == 2
        clock.advance(timedelta(seconds=31))
        assert (await h.client.get(_url(first["id"]), headers=owner.headers)).status_code == 200
        assert len(fake.requests) == 3
        async with h.store.transaction() as tx:
            assert len(await tx.audit.find(action=CAMERA_SNAPSHOT)) == 3


async def test_fleet_snapshot_shares_one_request_between_concurrent_callers() -> None:
    bus = InMemoryBus()
    async with harness(bus=bus) as h:
        fake = await _gateway(bus)
        fake.delay = 0.05
        owner = await h.owner()
        created = await h.register(owner)
        answers = await asyncio.gather(
            *(h.client.get(_url(created["id"]), headers=owner.headers) for _ in range(3))
        )
        assert [a.status_code for a in answers] == [200, 200, 200]
        assert len(fake.requests) == 1


async def test_fleet_snapshot_takes_the_first_frame_of_a_set() -> None:
    bus = InMemoryBus()
    async with harness(bus=bus) as h:
        fake = await _gateway(bus)
        frame = PhotoFrame(image_base64=base64.b64encode(JPEG).decode(), media_type="image/png")
        fake.reply = PhotoReply(frames=[frame])
        owner = await h.owner()
        created = await h.register(owner)
        response = await h.client.get(_url(created["id"]), headers=owner.headers)
        assert response.status_code == 200
        assert response.headers["content-type"] == "image/png"


async def test_fleet_snapshot_hides_foreign_and_ungranted_mowers() -> None:
    bus = InMemoryBus()
    async with harness(bus=bus) as h:
        fake = await _gateway(bus)
        owner = await h.owner()
        stranger = await h.owner()
        first = await h.register(owner)
        second = await h.register(owner)
        assert (await h.client.get(_url(first["id"]), headers=stranger.headers)).status_code == 404
        limited = await h.member(owner, "viewer", mower(UUID(first["id"])))
        assert (await h.client.get(_url(first["id"]), headers=limited.headers)).status_code == 200
        assert (await h.client.get(_url(second["id"]), headers=limited.headers)).status_code == 404
        assert len(fake.requests) == 1


async def test_fleet_snapshot_needs_the_plan_feature() -> None:
    bus = InMemoryBus()
    plans = FakeEntitlements(plan="crafted", features=["view", "control", "emergency_stop"])
    async with harness(bus=bus, entitlements=plans) as h:
        fake = await _gateway(bus)
        owner = await h.owner()
        created = await h.register(owner)
        response = await h.client.get(_url(created["id"]), headers=owner.headers)
        assert response.status_code == 403
        assert response.json()["type"] == "urn:mowdeck:problem:feature-not-in-plan"
        assert fake.requests == []


async def test_fleet_snapshot_maps_gateway_errors() -> None:
    bus = InMemoryBus()
    async with harness(
        bus=bus, camera_snapshot_timeout_seconds=5, camera_snapshot_min_interval_seconds=0
    ) as h:
        fake = await _gateway(bus)
        owner = await h.owner()
        created = await h.register(owner)
        url = _url(created["id"])

        def error(code: PhotoErrorCode) -> PhotoReply:
            return PhotoReply(error=PhotoError(code=code, detail=f"{code} here"))

        fake.reply = error(PhotoErrorCode.UNSUPPORTED)
        response = await h.client.get(url, headers=owner.headers)
        assert response.status_code == 409
        assert response.json()["type"] == "urn:mowdeck:problem:camera-unsupported"
        for code in (
            PhotoErrorCode.NOT_FOUND,
            PhotoErrorCode.NOT_CONNECTED,
            PhotoErrorCode.FAILED,
            PhotoErrorCode.TOO_LARGE,
        ):
            fake.reply = error(code)
            response = await h.client.get(url, headers=owner.headers)
            assert response.status_code == 503, code
            assert response.json()["type"] == "urn:mowdeck:problem:unavailable"
        async with h.store.transaction() as tx:
            assert await tx.audit.find(action=CAMERA_SNAPSHOT) == []


async def test_fleet_snapshot_without_any_gateway_is_unavailable() -> None:
    async with harness() as h:
        owner = await h.owner()
        created = await h.register(owner)
        response = await h.client.get(_url(created["id"]), headers=owner.headers)
        assert response.status_code == 503


class _Gateway:
    def __init__(self, reply: PhotoReply | Exception) -> None:
        self.reply = reply

    async def request(self, request: PhotoRequest) -> PhotoReply:
        if isinstance(self.reply, Exception):
            raise self.reply
        return self.reply


@pytest.mark.parametrize(
    "reply",
    [
        PhotoReply(image_base64="not base64!", media_type="image/jpeg"),
        TimeoutError("silent"),
        None,
    ],
)
async def test_fleet_snapshot_refuses_replies_without_a_readable_image(
    reply: PhotoReply | Exception | None,
) -> None:
    target: Any = SimpleNamespace(id=UUID(int=1), account_id=UUID(int=2))
    store: Any = None
    service = CameraService(
        store, store, MovableClock(), photos=_Gateway(reply) if reply is not None else None
    )
    with pytest.raises(Unavailable):
        await service._take_photo(target)
