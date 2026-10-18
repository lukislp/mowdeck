"""Frame sets for Vision: both front cameras of the same moment under one lease, the closest
frames, a silent or late camera that is left out instead of failing the set and the size cap
(the HTTP route is tested with the other frame tap routes)."""

import asyncio
from datetime import timedelta
from uuid import uuid4

import pytest
from frames_support import (
    FRONT_CAMERAS,
    JPEG,
    SET_T0,
    LiveVendor,
    TimedGrabber,
)
from mowdeck_contracts.v1 import PrincipalKind
from mowdeck_contracts.v1.camera import CameraInfo, CameraPlacement
from mowdeck_contracts.v1.principal import PrincipalRef
from mowdeck_contracts.v1.relay import RelayPurpose, VisionDemand, VisionFrameMissingReason
from relay_support import ONLINE, Harness, attach_request, key

from camera_relay.application.frames import (
    SET_FPS,
    Frame,
    FrameRequest,
    FrameSet,
    FrameTap,
    FrameTapError,
    closest_frames,
)
from camera_relay.application.service import vision_cameras
from camera_relay.domain.models import RelayKey

GATEWAY = PrincipalRef(subject=uuid4(), kind=PrincipalKind.SERVICE, display_name="device-gateway")
LUBA_CAMERAS = (
    CameraInfo(id="rear", name="Rear", uid=3, placement=CameraPlacement.REAR),
    CameraInfo(id="front_left", name="Front left", uid=1, placement=CameraPlacement.FRONT_LEFT),
    CameraInfo(id="front_right", name="Front right", uid=2, placement=CameraPlacement.FRONT_RIGHT),
)


def _harness(cameras: tuple[CameraInfo, ...] = LUBA_CAMERAS, **vendor: object) -> Harness:
    harness = Harness(vendor=LiveVendor(cameras=cameras, **vendor))  # type: ignore[arg-type]
    assert isinstance(harness.vendor, LiveVendor)
    harness.vendor.service = harness.service
    return harness


def _request(relay_key: RelayKey) -> FrameRequest:
    return FrameRequest(
        account_id=relay_key.account_id,
        mower_id=relay_key.mower_id,
        requested_by=GATEWAY,
        demand=VisionDemand.ERROR_ANALYSIS,
    )


def _tap(
    harness: Harness, grabber: TimedGrabber, *, window: float = 0.05, partner: float = 5.0
) -> FrameTap:
    return FrameTap(
        harness.service,
        grabber,
        ready_poll=0.01,
        retry_wait=0.01,
        set_window=window,
        partner_wait=partner,
    )


async def _set(
    tap: FrameTap, relay_key: RelayKey, *, timeout_s: float = 5, max_bytes: int = 512_000
) -> FrameSet:
    return await tap.snapshot_set(
        _request(relay_key), max_frames=2, max_width=640, max_bytes=max_bytes, timeout_s=timeout_s
    )


def _online(harness: Harness) -> RelayKey:
    relay_key = key()
    harness.repository.set_status(relay_key.mower_id, ONLINE)
    return relay_key


def _cameras(found: FrameSet) -> list[tuple[str, str | None, float]]:
    return [
        (
            item.camera.id,
            item.side,
            (item.frame.captured_at - SET_T0).total_seconds() * 1000,
        )
        for item in found.frames
    ]


async def _settle_later(harness: Harness, seconds: float = 20) -> None:
    """Let the camera settle time pass on the relay's clock once the source is live."""
    await asyncio.sleep(0.05)
    harness.clock.advance(seconds)


async def test_a_set_holds_one_lease_on_both_front_cameras_and_serves_them_left_first() -> None:
    harness = _harness()
    relay_key = _online(harness)
    grabber = TimedGrabber(offsets={"front_right": [40], "front_left": [0]})
    found = await _set(_tap(harness, grabber), relay_key)
    assert _cameras(found) == [("front_left", "left", 0), ("front_right", "right", 40)]
    assert found.skew_ms == 40
    assert found.missing == ()
    assert [item.camera.name for item in found.frames] == ["Front left", "Front right"]
    # One vendor session with both front cameras, never the listed rear one.
    assert [start.camera_ids for start in harness.vendor.starts] == [("front_left", "front_right")]
    assert sorted(camera for camera, _ in grabber.streams) == ["front_left", "front_right"]
    assert {fps for _, fps in grabber.streams} == {SET_FPS}
    # The lease lived exactly as long as the request; every reader is closed.
    assert grabber.open_readers == 0
    session = harness.repository.get(relay_key)
    assert session is not None and session.leases == {}
    opened = [
        envelope.payload
        for _, envelope in harness.events.published
        if envelope.type == "camera.relay.lease_opened"
    ]
    assert [(o.purpose, o.camera_ids) for o in opened] == [
        (RelayPurpose.VISION, ["front_left", "front_right"])
    ]


async def test_a_set_takes_the_frames_captured_closest_together() -> None:
    harness = _harness()
    relay_key = _online(harness)
    grabber = TimedGrabber(
        offsets={"front_left": [0, 100, 200, 300, 400], "front_right": [690, 790]}
    )
    found = await _set(_tap(harness, grabber), relay_key)
    assert _cameras(found) == [("front_left", "left", 400), ("front_right", "right", 690)]
    assert found.skew_ms == 290


async def test_frames_beyond_the_target_skew_are_served_after_the_window_with_their_skew() -> None:
    harness = _harness()
    relay_key = _online(harness)
    grabber = TimedGrabber(offsets={"front_left": [0, 100], "front_right": [600]})
    found = await _set(_tap(harness, grabber, window=0.05), relay_key)
    assert _cameras(found) == [("front_left", "left", 100), ("front_right", "right", 600)]
    assert found.skew_ms == 500, "the closest found; Vision decides whether it is a pair"


async def test_a_silent_camera_is_left_out_and_named_never_failing_the_set() -> None:
    harness = _harness(silent={"front_right"})
    relay_key = _online(harness)
    grabber = TimedGrabber(offsets={"front_left": [0]})
    settle = asyncio.create_task(_settle_later(harness))
    found = await _set(_tap(harness, grabber), relay_key)
    await settle
    assert _cameras(found) == [("front_left", "left", 0)]
    assert found.skew_ms is None
    assert [(m.camera.id, m.side, m.reason) for m in found.missing] == [
        ("front_right", "right", VisionFrameMissingReason.SILENT)
    ]
    assert [camera for camera, _ in grabber.streams] == ["front_left"], (
        "a silent camera's path is never read"
    )


async def test_a_camera_without_a_frame_in_time_is_left_out_after_the_partner_wait() -> None:
    harness = _harness()
    relay_key = _online(harness)
    grabber = TimedGrabber(offsets={"front_right": [0]})  # the left one publishes, no frame
    found = await _set(_tap(harness, grabber, partner=0.05), relay_key)
    assert _cameras(found) == [("front_right", "right", 0)]
    assert [(m.camera.id, m.reason) for m in found.missing] == [
        ("front_left", VisionFrameMissingReason.NO_FRAME)
    ]
    assert grabber.open_readers == 0
    session = harness.repository.get(relay_key)
    assert session is not None and session.leases == {}


async def test_no_frame_of_any_camera_times_out_without_a_lease_left() -> None:
    harness = _harness()
    relay_key = _online(harness)
    grabber = TimedGrabber()
    tap = _tap(harness, grabber)
    with pytest.raises(FrameTapError) as failed:
        await _set(tap, relay_key, timeout_s=0.2)
    assert failed.value.code == "timeout"
    await tap.drain()
    assert grabber.open_readers == 0
    session = harness.repository.get(relay_key)
    assert session is not None and session.leases == {}


async def test_when_both_front_cameras_are_silent_another_camera_is_tried() -> None:
    cameras = (
        *FRONT_CAMERAS,
        CameraInfo(id="front", name="Front", uid=5, placement=CameraPlacement.FRONT),
    )
    harness = _harness(cameras, silent={"front_left", "front_right"})
    relay_key = _online(harness)
    grabber = TimedGrabber(offsets={"front": [0]})
    settle = asyncio.create_task(_settle_later(harness))
    found = await _set(_tap(harness, grabber), relay_key)
    await settle
    assert _cameras(found) == [("front", None, 0)]
    session = harness.repository.get(relay_key)
    assert session is not None and session.leases == {}


async def test_a_mower_with_one_camera_gives_a_set_of_one_without_missing_cameras() -> None:
    single = (CameraInfo(id="cam", name="Camera", uid=1, placement=CameraPlacement.FRONT),)
    harness = _harness(single)
    relay_key = _online(harness)
    found = await _set(_tap(harness, TimedGrabber(offsets={"cam": [0]})), relay_key)
    assert _cameras(found) == [("cam", None, 0)]
    assert found.missing == ()


async def test_a_running_viewer_session_is_reused_and_gets_the_other_front_camera() -> None:
    harness = _harness()
    relay_key = _online(harness)
    viewer = await harness.service.attach(attach_request(relay_key, ["front_right"]))
    assert viewer.session is not None and viewer.session.relay is not None
    await asyncio.sleep(0.01)
    await harness.service.media_confirmed(viewer.session.relay.lease_id, "w", "front_right")
    grabber = TimedGrabber(offsets={"front_left": [0], "front_right": [20]})
    found = await _set(_tap(harness, grabber), relay_key)
    assert [item.camera.id for item in found.frames] == ["front_left", "front_right"]
    assert len(harness.vendor.starts) == 1, "no second vendor session"
    session = harness.repository.get(relay_key)
    assert session is not None
    assert [lease.purpose for lease in session.leases.values()] == [RelayPurpose.VIEWER]


async def test_large_frames_are_shrunk_and_a_frame_that_cannot_be_is_left_out() -> None:
    harness = _harness()
    relay_key = _online(harness)
    grabber = TimedGrabber(offsets={"front_left": [0], "front_right": [0]}, large={"front_left"})
    found = await _set(_tap(harness, grabber), relay_key, max_bytes=len(JPEG) + 10)
    assert [item.frame.data for item in found.frames] == [JPEG, JPEG]
    assert len(grabber.fitted) == 1

    harness = _harness()
    relay_key = _online(harness)
    grabber = TimedGrabber(
        offsets={"front_left": [0], "front_right": [0]}, large={"front_left"}, unfit=True
    )
    found = await _set(_tap(harness, grabber), relay_key, max_bytes=len(JPEG) + 10)
    assert [item.camera.id for item in found.frames] == ["front_right"]
    assert [(m.camera.id, m.reason) for m in found.missing] == [
        ("front_left", VisionFrameMissingReason.TOO_LARGE)
    ]

    harness = _harness()
    relay_key = _online(harness)
    grabber = TimedGrabber(
        offsets={"front_left": [0], "front_right": [0]},
        large={"front_left", "front_right"},
        unfit=True,
    )
    with pytest.raises(FrameTapError) as failed:
        await _set(_tap(harness, grabber), relay_key, max_bytes=len(JPEG) + 10)
    assert failed.value.code == "failed"


async def test_account_b_never_gets_a_set_of_account_a() -> None:
    mower = uuid4()
    account_a, account_b = uuid4(), uuid4()
    harness = _harness(owner={mower: account_a})
    harness.repository.set_status(mower, ONLINE)
    grabber = TimedGrabber(offsets={"front_left": [0], "front_right": [0]})
    tap = _tap(harness, grabber)
    await _set(tap, RelayKey(account_a, mower))
    read = list(grabber.streams)
    with pytest.raises(FrameTapError) as refused:
        await _set(tap, RelayKey(account_b, mower))
    assert refused.value.code == "forbidden"
    assert grabber.streams == read
    assert len(harness.vendor.starts) == 1


async def test_a_cancelled_set_closes_its_readers_and_its_lease() -> None:
    harness = _harness()
    relay_key = _online(harness)
    grabber = TimedGrabber(offsets={"front_left": [0]})  # waits for the right camera
    tap = _tap(harness, grabber)
    task = asyncio.create_task(_set(tap, relay_key))
    await asyncio.sleep(0.1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await tap.drain()
    assert grabber.open_readers == 0
    session = harness.repository.get(relay_key)
    assert session is not None and session.leases == {}


def test_the_closest_frames_prefer_the_smallest_spread_then_the_newest() -> None:
    def frame(ms: float) -> Frame:
        at = SET_T0 + timedelta(milliseconds=ms)
        return Frame(data=JPEG, received_at=at, captured_at=at)

    left = [frame(0), frame(100), frame(200)]
    right = [frame(90), frame(210)]
    chosen, spread = closest_frames([left, right])
    assert [f.captured_at for f in chosen] == [frame(200).captured_at, frame(210).captured_at]
    assert spread == pytest.approx(0.01)
    single, none = closest_frames([[frame(5), frame(7)]])
    assert [f.captured_at for f in single] == [frame(7).captured_at]
    assert none == 0


def test_two_vision_cameras_are_the_front_pair_whatever_else_publishes() -> None:
    harness = _harness()
    relay_key = _online(harness)
    session = harness.repository.get_or_create(relay_key)
    session.offer(LUBA_CAMERAS)
    now = harness.clock()
    assert vision_cameras(session, now, count=2) == ["front_left", "front_right"]
    assert vision_cameras(session, now, count=1) == ["front_left"]
    assert vision_cameras(session, now, ["front_left"], count=2) == ["front_right", "rear"]
    assert vision_cameras(session, now, ["front_left", "front_right", "rear"], count=2) == []
