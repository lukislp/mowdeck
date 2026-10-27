"""Camera photo of error and theft events: taken in the background, signed push URL, inbox image."""

import asyncio
import base64
import json
from dataclasses import replace
from datetime import timedelta
from typing import Any
from urllib.parse import parse_qs, urlsplit
from uuid import UUID, uuid4

import httpx
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from mowdeck_contracts.v1 import (
    NotificationKind,
    NotificationSeverity,
    PhotoError,
    PhotoErrorCode,
    PhotoFrame,
    PhotoReply,
    PhotoRequest,
    VisionDemand,
)
from mowdeck_contracts.v1.subjects import photo_subject
from mowdeck_service_kit.bus import BusMessage, InMemoryBus
from notifications_support import API, T0, Harness, ManualClock, Person, harness

from notifications.adapters.bus.photo import BusPhotoSource
from notifications.adapters.channels.apns import ApnsChannel, load_signing_key
from notifications.application.ports import CapturedPhoto
from notifications.domain.models import (
    IMAGE_KINDS,
    Delivery,
    Message,
    NotificationEvent,
    NotificationImage,
    TargetKind,
)
from notifications.domain.push_image import ImageLinks, derive_secret

JPEG = b"\xff\xd8\xff\xe0fake-jpeg-bytes"
BASE = "https://mowdeck.example.test"
SETTINGS: dict[str, Any] = {
    "notifications_app_url": BASE,
    "notifications_image_secret": "a-test-secret-of-some-length",
}


GOOD = CapturedPhoto(JPEG, "image/jpeg")


class FakePhotos:
    """A photo source the test releases by hand."""

    def __init__(self, photo: CapturedPhoto | None = GOOD) -> None:
        self.photo = photo
        self.calls: list[UUID] = []
        self.release = asyncio.Event()

    async def capture(self, mower_id: UUID, account_id: UUID) -> CapturedPhoto | None:
        self.calls.append(mower_id)
        await self.release.wait()
        return self.photo


def use(h: Harness, photos: FakePhotos) -> None:
    h.services.fanout._photos = photos


async def setup(h: Harness, photos: FakePhotos) -> tuple[Person, UUID]:
    owner = await h.owner()
    mower = await h.add_mower(owner)
    await h.subscribe_all(
        owner,
        enabled_kinds=["mower_stuck", "battery_low", "mower_lifted", "geofence_exit"],
    )
    use(h, photos)
    return owner, mower


def event(
    h: Harness,
    owner: Person,
    mower: UUID,
    kind: NotificationKind = NotificationKind.MOWER_STUCK,
    key: str = "a",
) -> NotificationEvent:
    return NotificationEvent.of(
        kind, mower, owner.account_id, h.clock.now, key, variant="error", reason="x", code="1"
    )


def query(url: str) -> tuple[str, dict[str, str]]:
    parts = urlsplit(url)
    return parts.path, {k: v[0] for k, v in parse_qs(parts.query).items()}


async def test_push_image_kinds_are_the_error_and_theft_kinds() -> None:
    assert {
        NotificationKind.MOWER_STUCK,
        NotificationKind.MOWER_LIFTED,
        NotificationKind.MOWER_LEFT_DOCK_UNEXPECTEDLY,
        NotificationKind.GEOFENCE_EXIT,
    } == IMAGE_KINDS


async def test_push_image_photo_is_taken_in_the_background_and_the_push_does_not_wait() -> None:
    photos = FakePhotos()
    async with harness(**SETTINGS) as h:
        owner, mower = await setup(h, photos)
        await h.services.fanout.dispatch([event(h, owner, mower)])
        # The delivery is queued and sent while the camera has not answered yet.
        assert await h.services.outbox.run_once() == 1
        assert len(h.channel.sent) == 1
        assert h.channel.sent[0].message.image_id is not None
        assert photos.calls == [mower]
        item = (await h.inbox(owner))[0]
        assert item["has_image"] is False
        photos.release.set()
        await h.services.fanout.drain()
        item = (await h.inbox(owner))[0]
        assert item["has_image"] is True
        response = await h.client.get(f"{API}/inbox/{item['id']}/image", headers=owner.headers)
        assert response.status_code == 200
        assert response.content == JPEG
        assert response.headers["content-type"] == "image/jpeg"


async def test_push_image_inbox_image_belongs_to_its_user() -> None:
    photos = FakePhotos()
    photos.release.set()
    async with harness(**SETTINGS) as h:
        owner, mower = await setup(h, photos)
        await h.services.fanout.dispatch([event(h, owner, mower)])
        await h.services.fanout.drain()
        item_id = (await h.inbox(owner))[0]["id"]
        stranger = await h.owner()
        response = await h.client.get(f"{API}/inbox/{item_id}/image", headers=stranger.headers)
        assert response.status_code == 404
        h.clock.advance(25 * 3600)
        response = await h.client.get(f"{API}/inbox/{item_id}/image", headers=owner.headers)
        assert response.status_code == 404


async def test_push_image_none_for_routine_kinds() -> None:
    photos = FakePhotos()
    photos.release.set()
    async with harness(**SETTINGS) as h:
        owner, mower = await setup(h, photos)
        routine = NotificationEvent.of(
            NotificationKind.BATTERY_LOW, mower, owner.account_id, h.clock.now, "b", percent=10
        )
        await h.services.fanout.dispatch([routine])
        await h.services.fanout.drain()
        assert photos.calls == []
        item = (await h.inbox(owner))[0]
        assert item["has_image"] is False


async def test_push_image_theft_alarm_gets_a_photo() -> None:
    photos = FakePhotos()
    photos.release.set()
    async with harness(**SETTINGS) as h:
        owner, mower = await setup(h, photos)
        ev = NotificationEvent.of(
            NotificationKind.MOWER_LIFTED, mower, owner.account_id, h.clock.now, "l"
        )
        await h.services.fanout.dispatch([ev])
        await h.services.fanout.drain()
        assert photos.calls == [mower]


async def test_push_image_budget_limits_photos_per_mower() -> None:
    photos = FakePhotos()
    photos.release.set()
    async with harness(
        **SETTINGS,
        notifications_image_daily_limit=2,
        notifications_image_min_interval_seconds=60,
    ) as h:
        owner, mower = await setup(h, photos)
        fanout = h.services.fanout
        await fanout.dispatch([event(h, owner, mower, key="1")])
        h.clock.advance(10)  # inside the minimum interval: no photo
        await fanout.dispatch([event(h, owner, mower, key="2")])
        h.clock.advance(120)
        await fanout.dispatch([event(h, owner, mower, key="3")])
        h.clock.advance(120)  # the daily limit of two is used up
        await fanout.dispatch([event(h, owner, mower, key="4")])
        await fanout.drain()
        assert len(photos.calls) == 2
        h.clock.advance(24 * 3600)
        await fanout.dispatch([event(h, owner, mower, key="5")])
        await fanout.drain()
        assert len(photos.calls) == 3


async def test_push_image_off_by_setting() -> None:
    photos = FakePhotos()
    photos.release.set()
    async with harness(**SETTINGS, notifications_image_enabled=False) as h:
        owner, mower = await setup(h, photos)
        await h.services.fanout.dispatch([event(h, owner, mower)])
        await h.services.fanout.drain()
        assert photos.calls == []
        assert h.services.push_images is None


async def test_push_image_failed_capture_is_stored_as_marker() -> None:
    photos = FakePhotos(photo=None)
    photos.release.set()
    async with harness(**SETTINGS) as h:
        owner, mower = await setup(h, photos)
        await h.services.fanout.dispatch([event(h, owner, mower)])
        await h.services.fanout.drain()
        assert (await h.inbox(owner))[0]["has_image"] is False
        stored = next(iter(h.store.data.images.values()))
        assert stored.data is None


async def test_push_image_too_large_photo_is_dropped() -> None:
    photos = FakePhotos(CapturedPhoto(b"x" * 2048, "image/jpeg"))
    photos.release.set()
    async with harness(**SETTINGS, notifications_image_max_bytes=1024) as h:
        owner, mower = await setup(h, photos)
        await h.services.fanout.dispatch([event(h, owner, mower)])
        await h.services.fanout.drain()
        assert next(iter(h.store.data.images.values())).data is None


def signed(h: Harness, item_id: UUID) -> tuple[str, dict[str, str]]:
    links = ImageLinks(derive_secret(SETTINGS["notifications_image_secret"]), BASE)
    return query(links.url(item_id, h.clock.now))


async def fetch(h: Harness, path: str, params: dict[str, str]) -> httpx.Response:
    return await h.client.get(path, params=params)  # no credentials at all


async def test_push_image_signed_url_lifecycle() -> None:
    photos = FakePhotos()
    async with harness(**SETTINGS) as h:
        owner, mower = await setup(h, photos)
        await h.services.fanout.dispatch([event(h, owner, mower)])
        item_id = UUID((await h.inbox(owner))[0]["id"])
        path, params = signed(h, item_id)
        assert path == f"{API}/push-images/{item_id}"
        assert (await fetch(h, path, params)).status_code == 404  # not ready yet
        photos.release.set()
        await h.services.fanout.drain()
        response = await fetch(h, path, params)
        assert response.status_code == 200
        assert response.content == JPEG
        assert response.headers["cache-control"] == "private, no-store"
        tampered = {**params, "e": str(int(params["e"]) + 600)}
        assert (await fetch(h, path, tampered)).status_code == 404
        other = f"{API}/push-images/{uuid4()}"
        assert (await fetch(h, other, params)).status_code == 404
        h.clock.advance(11 * 60)
        assert (await fetch(h, path, params)).status_code == 410  # expired link
        # a fresh link of the same notification works while the photo is kept
        _, fresh = signed(h, item_id)
        assert (await fetch(h, path, fresh)).status_code == 200


async def test_push_image_failed_capture_answers_gone_at_once() -> None:
    photos = FakePhotos(photo=None)
    photos.release.set()
    async with harness(**SETTINGS) as h:
        owner, mower = await setup(h, photos)
        await h.services.fanout.dispatch([event(h, owner, mower)])
        await h.services.fanout.drain()
        item_id = UUID((await h.inbox(owner))[0]["id"])
        path, params = signed(h, item_id)
        assert (await fetch(h, path, params)).status_code == 410


async def test_push_image_requests_are_rate_limited() -> None:
    photos = FakePhotos()
    async with harness(**SETTINGS) as h:
        owner, mower = await setup(h, photos)
        await h.services.fanout.dispatch([event(h, owner, mower)])
        item_id = UUID((await h.inbox(owner))[0]["id"])
        path, params = signed(h, item_id)
        codes = [(await fetch(h, path, params)).status_code for _ in range(45)]
        assert codes[:40] == [404] * 40
        assert codes[40:] == [429] * 5
        photos.release.set()
        await h.services.fanout.drain()


async def test_push_image_unconfigured_server_answers_404() -> None:
    async with harness() as h:  # no public URL, no key
        assert h.services.push_images is None
        response = await fetch(h, f"{API}/push-images/{uuid4()}", {"e": "1", "s": "x" * 20})
        assert response.status_code == 404


async def test_push_image_store_cleanup_and_bound() -> None:
    async with harness(**SETTINGS) as h:
        store = h.store
        mower, account = uuid4(), uuid4()

        def image(minutes: int, lifetime_hours: int = 1) -> NotificationImage:
            created = T0 + timedelta(minutes=minutes)
            return NotificationImage(
                uuid4(), account, mower, created, created + timedelta(hours=lifetime_hours),
                "image/jpeg", JPEG,
            )  # fmt: skip

        old = image(0)
        await store.put_image(old, keep=2)
        await store.put_image(image(5), keep=2)
        await store.put_image(image(10), keep=2)
        assert old.id not in store.data.images  # the oldest of three goes (keep = 2)
        assert len(store.data.images) == 2
        assert await store.prune_images(T0 + timedelta(hours=1, minutes=7)) == 1
        assert len(store.data.images) == 1
        await store.put_image(image(200), keep=5)  # an expired image goes with the next write
        assert len(store.data.images) == 1


def apns_channel(links: ImageLinks | None, clock: ManualClock) -> ApnsChannel:
    key = ec.generate_private_key(ec.SECP256R1())
    pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()
    return ApnsChannel(
        key=load_signing_key(pem),
        key_id="ABCDE12345",
        team_id="TEAM123456",
        bundle_id="app.mowdeck.mobile",
        clock=clock,
        http=httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200))),
        image_links=links,
    )


def apns_delivery(image_id: UUID | None) -> Delivery:
    notification_id = uuid4()
    return Delivery(
        id=uuid4(),
        user_id=uuid4(),
        account_id=uuid4(),
        channel="apns",
        message=Message(
            kind=NotificationKind.MOWER_STUCK,
            severity=NotificationSeverity.CRITICAL,
            title="Stuck",
            body="Front lawn",
            url="/m",
            tag="mower_stuck:1",
            mower_id=uuid4(),
            notification_id=notification_id,
            image_id=image_id,
        ),
        attempts=1,
        target_kind=TargetKind.APNS_DEVICE,
        target_id=uuid4(),
        target={"token": "ab" * 32},
        created_at=T0,
    )


async def test_push_image_apns_payload_carries_a_signed_url_not_the_photo() -> None:
    clock = ManualClock()
    links = ImageLinks(derive_secret("s"), BASE)
    channel = apns_channel(links, clock)
    delivery = apns_delivery(uuid4())
    data = json.loads(channel.payload(delivery))
    assert data["aps"]["mutable-content"] == 1
    url = data["image_url"]
    assert url.startswith(
        f"{BASE}/api/v1/notifications/push-images/{delivery.message.notification_id}"
    )
    assert str(delivery.user_id) not in url
    assert str(delivery.account_id) not in url
    assert str(delivery.message.mower_id) not in url
    _, params = query(url)
    assert int(params["e"]) == int((T0 + timedelta(minutes=10)).timestamp())
    assert len(json.dumps(data).encode()) < 4096
    # a retried delivery gets a link valid from the retry
    clock.advance(300)
    retried = json.loads(channel.payload(delivery))["image_url"]
    assert retried != url


async def test_push_image_apns_payload_without_photo_is_unchanged() -> None:
    clock = ManualClock()
    channel = apns_channel(ImageLinks(derive_secret("s"), BASE), clock)
    data = json.loads(channel.payload(apns_delivery(None)))
    assert "image_url" not in data
    assert "mutable-content" not in data["aps"]
    no_links = json.loads(apns_channel(None, clock).payload(apns_delivery(uuid4())))
    assert "image_url" not in no_links
    assert replace(apns_delivery(None).message, image_id=None).image_id is None


async def test_push_image_message_json_round_trip() -> None:
    delivery = apns_delivery(uuid4())
    assert Message.from_json(delivery.message.to_json()) == delivery.message
    legacy = delivery.message.to_json()
    del legacy["image_id"]  # a delivery queued by an older release
    assert Message.from_json(legacy).image_id is None


async def test_push_image_links_verify_expiry_and_tampering() -> None:
    links = ImageLinks(derive_secret("s"), BASE)
    nid = uuid4()
    _, params = query(links.url(nid, T0))
    expires, signature = int(params["e"]), params["s"]
    assert links.verify(nid, expires, signature, T0)
    assert not links.verify(nid, expires, signature, T0 + timedelta(minutes=10))
    assert not links.verify(uuid4(), expires, signature, T0)
    assert not links.verify(nid, expires + 1, signature, T0)
    assert links.genuine(nid, expires, signature)
    assert not ImageLinks(derive_secret("other"), BASE).genuine(nid, expires, signature)


async def test_push_image_bus_source_asks_the_gateway_and_decodes_the_reply() -> None:
    mower, account = uuid4(), uuid4()
    asked: list[PhotoRequest] = []

    async def gateway(message: BusMessage) -> None:
        request = message.model(PhotoRequest)
        asked.append(request)
        await message.respond(
            PhotoReply(image_base64=base64.b64encode(JPEG).decode(), media_type="image/jpeg")
        )

    bus = InMemoryBus()
    await bus.subscribe(photo_subject(mower), gateway)
    source = BusPhotoSource(bus, timeout_seconds=2, max_bytes=100_000)
    photo = await source.capture(mower, account)
    assert photo == CapturedPhoto(JPEG, "image/jpeg")
    assert asked[0].account_id == account
    assert asked[0].demand is VisionDemand.ERROR_ANALYSIS
    assert asked[0].max_frames == 1
    assert asked[0].max_bytes == 100_000
    # nobody answers for another mower: no photo, no exception
    assert await source.capture(uuid4(), account) is None


async def test_push_image_bus_source_gateway_error_and_frames() -> None:
    mower, account = uuid4(), uuid4()
    answers: list[PhotoReply] = [
        PhotoReply(error=PhotoError(code=PhotoErrorCode.NOT_CONNECTED, detail="asleep")),
        PhotoReply(
            frames=[
                PhotoFrame(image_base64=base64.b64encode(JPEG).decode(), media_type="image/jpeg")
            ]
        ),
    ]

    async def gateway(message: BusMessage) -> None:
        await message.respond(answers.pop(0))

    bus = InMemoryBus()
    await bus.subscribe(photo_subject(mower), gateway)
    source = BusPhotoSource(bus, timeout_seconds=2, max_bytes=100_000)
    assert await source.capture(mower, account) is None
    assert await source.capture(mower, account) == CapturedPhoto(JPEG, "image/jpeg")
