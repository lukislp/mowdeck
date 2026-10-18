"""The frame tap (Vision through the relay): leases for exactly the request, reuse of a running
session, the Vision limits, no lease leak on cancellation or timeout, account separation and
frame streams."""

import asyncio
import itertools
from datetime import timedelta
from uuid import uuid4

import pytest
from frames_support import FRONT_CAMERAS, FakeGrabber, LiveVendor
from mowdeck_contracts.v1 import PrincipalKind
from mowdeck_contracts.v1.camera import CameraInfo
from mowdeck_contracts.v1.principal import PrincipalRef
from mowdeck_contracts.v1.relay import (
    RelayLeaseCloseReason,
    RelayPurpose,
    VisionDemand,
)
from relay_support import ONLINE, Harness, attach_request, key, principal

from camera_relay.application.frames import (
    STREAM_HARD_CAP_SECONDS,
    FrameRequest,
    FrameTap,
    FrameTapError,
)
from camera_relay.domain.models import RelayKey, SessionState, VisionMode
from camera_relay.domain.policy import MowerStatus
from camera_relay.domain.session import _utc_day

GATEWAY = PrincipalRef(subject=uuid4(), kind=PrincipalKind.SERVICE, display_name="device-gateway")


def _harness(**vendor: object) -> Harness:
    harness = Harness(vendor=LiveVendor(cameras=FRONT_CAMERAS, **vendor))  # type: ignore[arg-type]
    assert isinstance(harness.vendor, LiveVendor)
    harness.vendor.service = harness.service
    return harness


def _request(
    relay_key: RelayKey,
    camera: str | None = None,
    demand: VisionDemand = VisionDemand.ROUTINE,
) -> FrameRequest:
    return FrameRequest(
        account_id=relay_key.account_id,
        mower_id=relay_key.mower_id,
        requested_by=GATEWAY,
        camera_id=camera,
        demand=demand,
    )


def _tap(harness: Harness, grabber: FakeGrabber, **kwargs: object) -> FrameTap:
    return FrameTap(harness.service, grabber, ready_poll=0.01, retry_wait=0.01, **kwargs)  # type: ignore[arg-type]


def _closed(harness: Harness) -> list[tuple[str, str]]:
    return [
        (envelope.payload.purpose.value, envelope.payload.reason.value)
        for _, envelope in harness.events.published
        if envelope.type == "camera.relay.lease_closed"
    ]


async def test_a_snapshot_holds_a_vision_lease_for_exactly_the_request() -> None:
    harness = _harness()
    relay_key = key()
    harness.repository.set_status(relay_key.mower_id, ONLINE)
    seen: list[tuple[RelayPurpose, VisionMode | None, str]] = []

    async def during(path: str) -> None:
        session = harness.repository.get(relay_key)
        assert session is not None
        lease = next(iter(session.leases.values()))
        seen.append((lease.purpose, lease.vision_mode, path))
        assert lease.principal == GATEWAY

    grabber = FakeGrabber(during=during)
    tap = _tap(harness, grabber)
    frame = await tap.snapshot(_request(relay_key), max_width=1280, max_bytes=512_000, timeout_s=5)

    assert frame.data.startswith(b"\xff\xd8")
    session = harness.repository.get(relay_key)
    assert session is not None
    relay_id = session.relay_id
    assert seen == [(RelayPurpose.VISION, VisionMode.SNAPSHOT, f"r/{relay_id}/front_left")]
    # The default camera is the first one with a publisher uid; only it was started.
    assert harness.vendor.starts[0].camera_ids == ("front_left",)
    assert session.leases == {}
    assert _closed(harness) == [("vision", "detached")]
    # The session drains for the grace and then stops and is released.
    assert session.state is SessionState.DRAINING
    harness.clock.advance(15)
    await harness.service.tick()
    assert harness.vendor.stops and harness.vendor.stops[0].release is True


async def test_a_snapshot_of_a_named_camera_waits_for_its_path() -> None:
    harness = _harness()
    relay_key = key()
    harness.repository.set_status(relay_key.mower_id, ONLINE)
    grabber = FakeGrabber(unavailable=3)
    tap = _tap(harness, grabber)
    await tap.snapshot(
        _request(relay_key, "front_right"), max_width=640, max_bytes=512_000, timeout_s=5
    )
    assert len(grabber.snapshots) == 4
    assert all(path.endswith("/front_right") for path in grabber.snapshots)


async def test_a_running_viewer_session_is_reused_without_a_second_vendor_session() -> None:
    harness = _harness()
    relay_key = key()
    harness.repository.set_status(relay_key.mower_id, ONLINE)
    service = harness.service
    viewer = await service.attach(attach_request(relay_key, ["front_left", "front_right"]))
    assert viewer.session is not None and viewer.session.relay is not None
    await asyncio.sleep(0.01)  # the source goes live
    await service.media_confirmed(viewer.session.relay.lease_id, "reader-1", "front_left")
    tap = _tap(harness, FakeGrabber())
    await tap.snapshot(_request(relay_key), max_width=640, max_bytes=512_000, timeout_s=5)

    assert len(harness.vendor.offers) == 1
    assert len(harness.vendor.starts) == 1
    session = harness.repository.get(relay_key)
    assert session is not None
    assert session.state is SessionState.LIVE
    assert list(session.leases) == [viewer.session.relay.lease_id]
    assert session.vision_used_on(_utc_day(harness.clock())) == timedelta(0)


@pytest.mark.parametrize(
    ("status", "code"),
    [
        (MowerStatus(online=True, battery_percent=10, charging=False), "budget"),
        (MowerStatus(online=True, battery_percent=90, quota_remaining_percent=5), "budget"),
        (MowerStatus(online=False, battery_percent=90), "unavailable"),
        (MowerStatus(online=True, asleep=True, battery_percent=90), "unavailable"),
        (None, "unavailable"),
    ],
)
async def test_vision_limits_refuse_without_a_viewer(status: MowerStatus | None, code: str) -> None:
    harness = _harness()
    relay_key = key()
    if status is not None:
        harness.repository.set_status(relay_key.mower_id, status)
    grabber = FakeGrabber()
    with pytest.raises(FrameTapError) as refused:
        await _tap(harness, grabber).snapshot(
            _request(relay_key), max_width=640, max_bytes=512_000, timeout_s=5
        )
    assert refused.value.code == code
    assert harness.vendor.starts == []
    assert grabber.snapshots == []


async def test_a_charging_mower_below_the_battery_limit_is_allowed() -> None:
    harness = _harness()
    relay_key = key()
    harness.repository.set_status(
        relay_key.mower_id, MowerStatus(online=True, battery_percent=8, charging=True)
    )
    await _tap(harness, FakeGrabber()).snapshot(
        _request(relay_key), max_width=640, max_bytes=512_000, timeout_s=5
    )


async def test_the_video_budget_of_the_offer_refuses_and_releases_the_offered_session() -> None:
    harness = _harness(quota_remaining_percent=4.0)
    relay_key = key()
    harness.repository.set_status(relay_key.mower_id, MowerStatus(online=True))
    with pytest.raises(FrameTapError) as refused:
        await _tap(harness, FakeGrabber()).snapshot(
            _request(relay_key), max_width=640, max_bytes=512_000, timeout_s=5
        )
    assert refused.value.code == "budget"
    assert "video budget" in refused.value.detail
    assert harness.vendor.discarded == [relay_key]
    assert harness.vendor.starts == []
    status = harness.repository.status_of(relay_key.mower_id)
    assert status is not None and status.quota_remaining_percent == 4.0


async def test_the_daily_vision_minutes_refuse_further_frames() -> None:
    harness = _harness()
    relay_key = key()
    harness.repository.set_status(relay_key.mower_id, ONLINE)
    session = harness.repository.get_or_create(relay_key)
    session._vision_by_day[_utc_day(harness.clock())] = timedelta(minutes=20)
    with pytest.raises(FrameTapError) as refused:
        await _tap(harness, FakeGrabber()).snapshot(
            _request(relay_key), max_width=640, max_bytes=512_000, timeout_s=5
        )
    assert refused.value.code == "budget"
    assert "vision minutes" in refused.value.detail


async def test_a_timeout_closes_the_lease() -> None:
    harness = _harness()
    relay_key = key()
    harness.repository.set_status(relay_key.mower_id, ONLINE)
    tap = _tap(harness, FakeGrabber(hang=True))
    with pytest.raises(FrameTapError) as timed_out:
        await tap.snapshot(_request(relay_key), max_width=640, max_bytes=512_000, timeout_s=0.2)
    assert timed_out.value.code == "timeout"
    session = harness.repository.get(relay_key)
    assert session is not None and session.leases == {}
    assert _closed(harness) == [("vision", "detached")]


async def test_a_source_that_never_goes_live_times_out_without_a_lease_left() -> None:
    harness = _harness(go_live=False)
    relay_key = key()
    harness.repository.set_status(relay_key.mower_id, ONLINE)
    with pytest.raises(FrameTapError) as timed_out:
        await _tap(harness, FakeGrabber()).snapshot(
            _request(relay_key), max_width=640, max_bytes=512_000, timeout_s=0.2
        )
    assert timed_out.value.code == "timeout"
    session = harness.repository.get(relay_key)
    assert session is not None and session.leases == {}


async def test_a_cancelled_request_never_leaks_its_lease() -> None:
    harness = _harness()
    relay_key = key()
    harness.repository.set_status(relay_key.mower_id, ONLINE)
    grabbing = asyncio.Event()

    async def during(path: str) -> None:
        del path
        grabbing.set()

    tap = _tap(harness, FakeGrabber(hang=True, during=during))
    task = asyncio.create_task(
        tap.snapshot(_request(relay_key), max_width=640, max_bytes=512_000, timeout_s=10)
    )
    await grabbing.wait()
    session = harness.repository.get(relay_key)
    assert session is not None and len(session.leases) == 1
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await tap.drain()
    assert session.leases == {}
    assert _closed(harness) == [("vision", "detached")]


async def test_account_b_never_gets_frames_of_account_a() -> None:
    mower = uuid4()
    account_a, account_b = uuid4(), uuid4()
    harness = _harness(owner={mower: account_a})
    harness.repository.set_status(mower, ONLINE)
    grabber = FakeGrabber()
    tap = _tap(harness, grabber)
    key_a = RelayKey(account_a, mower)
    await tap.snapshot(_request(key_a), max_width=640, max_bytes=512_000, timeout_s=5)
    session_a = harness.repository.get(key_a)
    assert session_a is not None and session_a.relay_id is not None
    paths_of_a = list(grabber.snapshots)

    with pytest.raises(FrameTapError) as refused:
        await tap.snapshot(
            _request(RelayKey(account_b, mower)), max_width=640, max_bytes=512_000, timeout_s=5
        )
    assert refused.value.code == "forbidden"
    assert grabber.snapshots == paths_of_a  # nothing of account A was read for B
    assert len(harness.vendor.starts) == 1
    session_b = harness.repository.get(RelayKey(account_b, mower))
    assert session_b is None or (session_b.relay_id is None and not session_b.leases)


async def test_a_stream_yields_frames_renews_and_detaches_when_closed() -> None:
    harness = _harness()
    relay_key = key()
    harness.repository.set_status(relay_key.mower_id, ONLINE)
    ticks = itertools.count(step=6.0)
    grabber = FakeGrabber(stream_frames=4)
    tap = _tap(harness, grabber, monotonic=lambda: next(ticks))
    stream = await tap.open_stream(_request(relay_key), fps=2, max_width=640)
    session = harness.repository.get(relay_key)
    assert session is not None
    lease = next(iter(session.leases.values()))
    assert (lease.purpose, lease.vision_mode) == (RelayPurpose.VISION, VisionMode.STREAM)
    received = []
    async for frame in stream.frames():
        received.append(frame)
        if len(received) == 3:
            break
    await stream.close()
    assert len(received) == 3
    assert grabber.streams[0][1] == 2  # the requested rate
    assert grabber.closed_streams == 1  # the reader was ended
    assert session.leases == {}
    assert _closed(harness) == [("vision", "detached")]


async def test_a_stream_is_capped_at_300_seconds() -> None:
    harness = _harness()
    relay_key = key()
    harness.repository.set_status(relay_key.mower_id, ONLINE)
    now = [0.0]

    def monotonic() -> float:
        now[0] += 100.0
        return now[0]

    grabber = FakeGrabber(stream_frames=50)
    tap = _tap(harness, grabber, monotonic=monotonic)
    stream = await tap.open_stream(_request(relay_key), fps=5, max_width=640)
    frames = [frame async for frame in stream.frames()]
    assert 0 < len(frames) < 50
    assert grabber.streams[0][2] <= STREAM_HARD_CAP_SECONDS
    assert stream.closed
    session = harness.repository.get(relay_key)
    assert session is not None and session.leases == {}


async def test_a_stream_ends_when_its_lease_is_closed_by_a_limit() -> None:
    harness = _harness()
    relay_key = key()
    harness.repository.set_status(relay_key.mower_id, ONLINE)

    async def offline() -> None:
        await harness.service.mower_status_changed(
            relay_key.mower_id, MowerStatus(online=False, battery_percent=50)
        )

    grabber = FakeGrabber(stream_frames=2, after_stream=offline)
    tap = _tap(harness, grabber)
    stream = await tap.open_stream(_request(relay_key), fps=1, max_width=640)
    frames = [frame async for frame in stream.frames()]
    assert len(frames) == 2
    assert len(grabber.streams) == 1
    assert ("vision", RelayLeaseCloseReason.SESSION_STOPPED.value) in _closed(harness)


async def test_a_refused_stream_opens_nothing() -> None:
    harness = _harness()
    relay_key = key()
    harness.repository.set_status(relay_key.mower_id, MowerStatus(online=True, battery_percent=5))
    with pytest.raises(FrameTapError) as refused:
        await _tap(harness, FakeGrabber()).open_stream(_request(relay_key), fps=1, max_width=640)
    assert refused.value.code == "budget"


async def test_the_default_camera_is_the_first_with_a_publisher_uid() -> None:
    cameras = (
        CameraInfo(id="rear", name="Rear"),
        CameraInfo(id="left", name="Left", uid=7),
        CameraInfo(id="right", name="Right", uid=8),
    )
    harness = _harness()
    assert isinstance(harness.vendor, LiveVendor)
    harness.vendor.cameras = cameras
    relay_key = key()
    harness.repository.set_status(relay_key.mower_id, ONLINE)
    grabber = FakeGrabber()
    await _tap(harness, grabber).snapshot(
        _request(relay_key), max_width=640, max_bytes=512_000, timeout_s=5
    )
    assert grabber.snapshots[0].endswith("/left")


async def test_a_viewer_lease_of_the_user_is_not_the_gateways() -> None:
    """Strict leases: the frame lease belongs to the requesting service, not to a user."""
    harness = _harness()
    relay_key = key()
    harness.repository.set_status(relay_key.mower_id, ONLINE)
    owners: list[PrincipalRef] = []

    async def during(path: str) -> None:
        del path
        session = harness.repository.get(relay_key)
        assert session is not None
        owners.extend(lease.principal for lease in session.leases.values())

    await _tap(harness, FakeGrabber(during=during)).snapshot(
        _request(relay_key), max_width=640, max_bytes=512_000, timeout_s=5
    )
    assert owners == [GATEWAY]
    assert owners[0] != principal()


@pytest.mark.parametrize(
    "status",
    [
        MowerStatus(online=True, asleep=True, battery_percent=80),
        MowerStatus(online=True, battery_percent=5),
        MowerStatus(online=True, battery_percent=80, quota_remaining_percent=2),
    ],
)
async def test_an_error_analysis_wakes_a_sleeping_mower_and_ignores_the_budget(
    status: MowerStatus,
) -> None:
    harness = _harness()
    relay_key = key()
    harness.repository.set_status(relay_key.mower_id, status)
    tap = _tap(harness, FakeGrabber())
    with pytest.raises(FrameTapError):
        await tap.snapshot(_request(relay_key), max_width=640, max_bytes=512_000, timeout_s=5)
    frame = await tap.snapshot(
        _request(relay_key, demand=VisionDemand.ERROR_ANALYSIS),
        max_width=640,
        max_bytes=512_000,
        timeout_s=5,
    )
    assert frame.data
    assert len(harness.vendor.starts) == 1


async def test_an_error_analysis_never_reaches_a_mower_without_connection() -> None:
    harness = _harness()
    relay_key = key()
    harness.repository.set_status(relay_key.mower_id, MowerStatus(online=False))
    with pytest.raises(FrameTapError) as refused:
        await _tap(harness, FakeGrabber()).snapshot(
            _request(relay_key, demand=VisionDemand.ERROR_ANALYSIS),
            max_width=640,
            max_bytes=512_000,
            timeout_s=5,
        )
    assert refused.value.code == "unavailable"
    assert harness.vendor.starts == []
