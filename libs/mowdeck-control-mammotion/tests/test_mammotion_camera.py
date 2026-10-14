"""Live camera over the Mammotion cloud (ADR 0032) on a fake PyMammotion port."""

# ruff: noqa: S106 - placeholder tokens, not credentials

import base64
import struct
import zlib
from datetime import UTC, datetime, timedelta

import pytest
from mammotion_support import NOW, ble, cloud
from mowdeck_contracts.v1 import CameraAccess, CameraPlacement, CameraProvider
from mowdeck_control import CameraCapable, NotConnected, camera_session_of
from mowdeck_control_mammotion import mapping
from mowdeck_control_mammotion.fake import FakeMammotionPort
from mowdeck_control_mammotion.port import (
    Camera,
    NoTransportAvailableError,
    StreamSubscriptionResponse,
    VideoResourceResponse,
)

ISSUED = int(NOW.timestamp()) - 60


def _string(value: str) -> bytes:
    raw = value.encode()
    return struct.pack("<H", len(raw)) + raw


def agora_token(*, issued: int = ISSUED, expire: int = 3600, privilege: int = 1800) -> str:
    """A syntactically valid AccessToken2 (``007``) with one RTC service; not signed."""
    content = (
        _string("signature")
        + _string("app-id")
        + struct.pack("<III", issued, expire, 7)
        + struct.pack("<H", 1)  # one service
        + struct.pack("<H", 1)  # RTC
        + struct.pack("<H", 1)  # one privilege
        + struct.pack("<HI", 1, privilege)  # join channel
        + _string("channel-x")
        + _string("4242")
    )
    return "007" + base64.b64encode(zlib.compress(content)).decode()


def subscription(**changes: object) -> StreamSubscriptionResponse:
    values: dict[str, object] = {
        "appid": "app-id",
        "openEncrypt": 0,
        "cameras": [Camera(cameraId=0, token="cam-0"), Camera(cameraId=1, token="cam-1")],
        "channelName": "channel-x",
        "areaCode": "GLOB",
        "token": agora_token(),
        "uid": 4242,
    }
    values.update(changes)
    return StreamSubscriptionResponse(**values)  # type: ignore[arg-type]


def video(total: int = 36_000, available: int = 33_000) -> VideoResourceResponse:
    return VideoResourceResponse(
        id="1",
        deviceId="iot",
        deviceName="Luba-VPDRYRUN",
        cycleType=1,
        usageYearMonth="2026-10",
        totalTime=total,
        availableTime=available,
    )


async def test_mammotion_cloud_camera_session_maps_the_stream_token() -> None:
    port = FakeMammotionPort()
    port.stream = subscription()
    port.video = video()
    conn, _ = cloud(port)
    await conn.connect()
    assert (await conn.capabilities()).camera is CameraAccess.CLOUD
    assert isinstance(conn, CameraCapable)
    session = await camera_session_of(conn)
    assert session is not None
    assert session.provider is CameraProvider.AGORA
    assert (session.app_id, session.channel, session.uid) == ("app-id", "channel-x", 4242)
    assert [(c.id, c.name, c.uid, c.placement) for c in session.cameras] == [
        ("0", "Front left", 1, CameraPlacement.FRONT_LEFT),
        ("1", "Front right", 2, CameraPlacement.FRONT_RIGHT),
    ]
    assert session.encryption is None
    # The privilege (1800 s) ends before the token (3600 s).
    assert session.expires_at == datetime.fromtimestamp(ISSUED + 1800, tz=UTC)
    assert session.quota is not None
    assert (session.quota.used_minutes, session.quota.total_minutes) == (50.0, 600.0)
    assert port.stream_requests == 1
    # Both front cameras are asked for (cameraStates 1, 1), so both uids publish.
    assert port.stream_all_cameras == [True]


async def test_mammotion_cloud_camera_can_ask_for_the_front_left_camera_only() -> None:
    port = FakeMammotionPort()
    port.stream = subscription()
    conn, _ = cloud(port, all_cameras=False)
    await conn.connect()
    assert await camera_session_of(conn) is not None
    assert port.stream_all_cameras == [False]


async def test_mammotion_cloud_camera_session_carries_encryption() -> None:
    port = FakeMammotionPort()
    port.stream = subscription(openEncrypt=1, key="k" * 32, salt="c2FsdA==")
    conn, _ = cloud(port)
    await conn.connect()
    session = await conn.get_camera_session()
    assert session is not None and session.encryption is not None
    assert session.encryption.mode == "aes-256-gcm2"
    assert session.encryption.key == "k" * 32
    assert session.encryption.salt == "c2FsdA=="
    assert session.quota is None
    assert "k" * 32 not in repr(session)


async def test_mammotion_cloud_camera_session_without_token_is_unavailable() -> None:
    port = FakeMammotionPort()
    conn, _ = cloud(port)
    await conn.connect()
    with pytest.raises(NotConnected, match="no stream token"):
        await conn.get_camera_session()
    port.stream_error = NoTransportAvailableError("down")
    with pytest.raises(NotConnected, match="not open"):
        await conn.get_camera_session()
    port.stream_error = RuntimeError("HTTP 500 with secrets")
    with pytest.raises(NotConnected, match="refused") as caught:
        await conn.get_camera_session()
    assert "secrets" not in str(caught.value)


async def test_mammotion_cloud_camera_ignores_a_failing_video_budget() -> None:
    port = FakeMammotionPort()
    port.stream = subscription()
    port.video_error = RuntimeError("budget endpoint down")
    conn, _ = cloud(port)
    await conn.connect()
    session = await conn.get_camera_session()
    assert session is not None and session.quota is None


async def test_mammotion_luba1_has_no_camera() -> None:
    port = FakeMammotionPort(device_name="Luba-ABCDEF", product_key="a1UBFdq6nNz")
    port.stream = subscription()
    conn, _ = cloud(port)
    await conn.connect()
    assert (await conn.capabilities()).camera is CameraAccess.NONE
    assert await conn.get_camera_session() is None
    assert port.stream_requests == 0


async def test_mammotion_ble_bridge_answers_no_camera() -> None:
    port = FakeMammotionPort()
    port.stream = subscription()
    conn, _ = ble(port)
    await conn.connect()
    assert (await conn.capabilities()).camera is CameraAccess.NONE
    assert await conn.get_camera_session() is None
    assert port.stream_requests == 0


def test_mammotion_camera_expiry_falls_back_for_unreadable_tokens() -> None:
    session = mapping.camera_session_of(subscription(token="not-a-token"), now=NOW)
    assert session.expires_at == NOW + timedelta(seconds=mapping.DEFAULT_CAMERA_SESSION_S)
    expired = mapping.camera_session_of(
        subscription(token=agora_token(issued=ISSUED - 10_000, expire=60, privilege=0)), now=NOW
    )
    assert expired.expires_at == NOW + timedelta(seconds=60)
    long = mapping.camera_session_of(
        subscription(token=agora_token(expire=10 * 86_400, privilege=0)), now=NOW
    )
    assert long.expires_at == NOW + timedelta(seconds=mapping.MAX_CAMERA_SESSION_S)


def test_mammotion_camera_names_unknown_slots_by_position() -> None:
    session = mapping.camera_session_of(
        subscription(cameras=[Camera(cameraId=7, token="x")]), now=NOW
    )
    assert [(c.id, c.name, c.uid, c.placement) for c in session.cameras] == [
        ("7", "Camera 1", None, None)
    ]
    rear = mapping.camera_session_of(subscription(cameras=[Camera(cameraId=2, token="x")]), now=NOW)
    assert [(c.name, c.uid, c.placement) for c in rear.cameras] == [
        ("Rear", 3, CameraPlacement.REAR)
    ]
    assert mapping.camera_quota_of(video(total=0)) is None


def test_mammotion_camera_one_based_ids_map_to_front_left_and_right() -> None:
    """The owner's LUBA 2 AWD X lists cameraId 1, 2, 3: the picker showed "Front rechts" next
    to "Kamera 1" because uid 1 (front left) was not listed."""
    session = mapping.camera_session_of(
        subscription(
            cameras=[
                Camera(cameraId=1, token="a"),
                Camera(cameraId=2, token="b"),
                Camera(cameraId=3, token="c"),
            ]
        ),
        now=NOW,
    )
    assert [(c.id, c.name, c.uid) for c in session.cameras] == [
        ("0", "Front left", 1),
        ("1", "Front right", 2),
        ("2", "Rear", 3),
    ]
    zero_based = mapping.camera_session_of(
        subscription(cameras=[Camera(cameraId=0, token="a"), Camera(cameraId=1, token="b")]),
        now=NOW,
    )
    assert [(c.name, c.uid) for c in zero_based.cameras] == [("Front left", 1), ("Front right", 2)]
