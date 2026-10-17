# SPDX-License-Identifier: Apache-2.0
"""The Agora adapter against a stand-in for the wrapper's modules (no SDK in CI)."""

import base64
from dataclasses import dataclass, field
from enum import IntEnum
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest

from agora_bridge.agora_sdk import ENCRYPTION_MODES, AgoraSdk
from agora_bridge.protocol import CODEC_H264, CODEC_H265, Encryption, Join
from agora_bridge.sdk import SdkError


class ChannelProfileType(IntEnum):
    CHANNEL_PROFILE_COMMUNICATION = 0
    CHANNEL_PROFILE_LIVE_BROADCASTING = 1


class ClientRoleType(IntEnum):
    CLIENT_ROLE_BROADCASTER = 1
    CLIENT_ROLE_AUDIENCE = 2


class VideoStreamType(IntEnum):
    VIDEO_STREAM_HIGH = 0


class AgoraServiceConfig:
    appid = ""
    enable_video = 0
    channel_profile: Any = None


@dataclass
class Kw:
    """Stands in for the wrapper's keyword-only dataclasses."""

    kwargs: dict[str, Any]

    def __getattr__(self, name: str) -> Any:
        return self.kwargs[name]


def kw(**kwargs: Any) -> Kw:
    return Kw(kwargs)


@dataclass
class FakeService:
    result: int = 0
    configs: list[Any] = field(default_factory=list)
    released: int = 0

    def initialize(self, config: Any) -> int:
        self.configs.append(config)
        return self.result

    def release(self) -> None:
        self.released += 1


@dataclass
class FakeLocalUser:
    subscribed: list[tuple[str, Any]] = field(default_factory=list)
    intra: list[str] = field(default_factory=list)

    def subscribe_video(self, uid: str, options: Any) -> int:
        self.subscribed.append((uid, options))
        return 0

    def send_intra_request(self, uid: str) -> int:
        self.intra.append(uid)
        return 0


class FakeConnection:
    last: "FakeConnection | None" = None
    connect_result = 0

    def __init__(self, service: Any, config: Any, publish: Any) -> None:
        self.conn_handle = object()
        self.config, self.publish = config, publish
        self.observers: list[Any] = []
        self.encryption: list[tuple[int, Any]] = []
        self.connected: list[tuple[str, str, str]] = []
        self.tokens: list[str] = []
        self.user = FakeLocalUser()
        self.disconnected = self.released = 0
        FakeConnection.last = self

    def register_observer(self, observer: Any) -> int:
        self.observers.append(observer)
        return 0

    register_local_user_observer = register_observer
    register_video_encoded_frame_observer = register_observer

    def enable_encryption(self, enabled: int, config: Any) -> int:
        self.encryption.append((enabled, config))
        return 0

    def get_local_user(self) -> FakeLocalUser:
        return self.user

    def connect(self, token: str, channel: str, uid: str) -> int:
        self.connected.append((token, channel, uid))
        return self.connect_result

    def renew_token(self, token: str) -> int:
        self.tokens.append(token)
        return 0

    def disconnect(self) -> int:
        self.disconnected += 1
        return 0

    def release(self) -> None:
        self.released += 1


class Base:
    """The wrapper's observer base classes: every callback is a no-op."""


def _modules(service: FakeService) -> dict[str, ModuleType]:
    agora_base: Any = SimpleNamespace(
        ChannelProfileType=ChannelProfileType,
        ClientRoleType=ClientRoleType,
        VideoStreamType=VideoStreamType,
        AgoraServiceConfig=AgoraServiceConfig,
        RTCConnConfig=kw,
        RtcConnectionPublishConfig=kw,
        EncryptionConfig=kw,
        VideoSubscriptionOptions=kw,
    )
    return {
        "agora_base": agora_base,
        "agora_service": SimpleNamespace(AgoraService=lambda: service),
        "rtc_connection": SimpleNamespace(RTCConnection=FakeConnection),
        "rtc_connection_observer": SimpleNamespace(IRTCConnectionObserver=Base),
        "local_user_observer": SimpleNamespace(IRTCLocalUserObserver=Base),
        "video_encoded_frame_observer": SimpleNamespace(IVideoEncodedFrameObserver=Base),
    }


@dataclass
class Listener:
    calls: list[tuple[Any, ...]] = field(default_factory=list)

    def connected(self) -> None:
        self.calls.append(("connected",))

    def connection_failed(self, detail: str) -> None:
        self.calls.append(("failed", detail))

    def user_joined(self, uid: int) -> None:
        self.calls.append(("joined", uid))

    def user_left(self, uid: int) -> None:
        self.calls.append(("left", uid))

    def video_state(self, uid: int, state: str) -> None:
        self.calls.append(("state", uid, state))

    def token_will_expire(self) -> None:
        self.calls.append(("expire",))

    def frame(self, uid: int, codec: int, keyframe: bool, capture_ms: int, data: bytes) -> None:
        self.calls.append(("frame", uid, codec, keyframe, capture_ms, data))

    def warning(self, code: str, detail: str) -> None:
        self.calls.append(("warning", code, detail))


SALT = base64.b64encode(bytes(range(32))).decode()


def _join(**changes: Any) -> Join:
    values: dict[str, Any] = {
        "app_id": "app",
        "channel": "chan",
        "token": "tok",
        "uid": 777,
        "encryption": Encryption("aes-256-gcm2", "key", SALT),
    }
    values.update(changes)
    return Join(**values)


def _sdk(tmp_path: Path, service: FakeService | None = None) -> tuple[AgoraSdk, FakeService]:
    service = service or FakeService()
    workdir = tmp_path / "work"
    workdir.mkdir(parents=True)
    return AgoraSdk(_modules(service), workdir), service


def test_join_is_a_receive_only_member_with_encryption(tmp_path: Path) -> None:
    sdk, service = _sdk(tmp_path)
    sdk.join(_join(), Listener())
    config = service.configs[0]
    assert config.appid == "app" and config.enable_video == 1
    assert config.log_path.startswith(str(tmp_path / "work"))
    conn = FakeConnection.last
    assert conn is not None
    assert conn.config.client_role_type is ClientRoleType.CLIENT_ROLE_AUDIENCE
    assert conn.config.auto_subscribe_video == 0 and conn.config.auto_subscribe_audio == 0
    assert conn.config.channel_profile is ChannelProfileType.CHANNEL_PROFILE_COMMUNICATION
    assert conn.publish.is_publish_audio is False and conn.publish.is_publish_video is False
    enabled, encryption = conn.encryption[0]
    assert enabled == 1 and encryption.encryption_mode == ENCRYPTION_MODES["aes-256-gcm2"] == 8
    assert encryption.encryption_kdf_salt == bytearray(range(32))
    assert conn.connected == [("tok", "chan", "777")]
    assert sdk.subscribe(2) == 0
    uid, options = conn.user.subscribed[0]
    assert uid == "2" and options.encodedFrameOnly is True
    assert sdk.keyframe(2) == 0 and conn.user.intra == ["2"]
    assert sdk.renew("tok2") == 0 and conn.tokens == ["tok2"]
    sdk.leave()
    sdk.leave()
    assert (conn.disconnected, conn.released, service.released) == (1, 1, 1)
    assert not (tmp_path / "work").exists()


def test_the_live_profile_and_no_encryption(tmp_path: Path) -> None:
    sdk, _ = _sdk(tmp_path)
    sdk.join(_join(encryption=None, profile="live"), Listener())
    conn = FakeConnection.last
    assert conn is not None and conn.encryption == []
    assert conn.config.channel_profile is ChannelProfileType.CHANNEL_PROFILE_LIVE_BROADCASTING


@pytest.mark.parametrize(
    ("change", "match"),
    [
        ({"encryption": Encryption("rot13", "k")}, "unsupported encryption mode"),
        ({"encryption": Encryption("aes-256-gcm2", "k", "%%%")}, "not base64"),
    ],
)
def test_bad_encryption_settings_refuse_the_join(
    tmp_path: Path, change: dict[str, Any], match: str
) -> None:
    sdk, _ = _sdk(tmp_path)
    with pytest.raises(SdkError, match=match):
        sdk.join(_join(**change), Listener())


def test_sdk_refusals_are_errors_without_secrets(tmp_path: Path) -> None:
    sdk, _ = _sdk(tmp_path, FakeService(result=-1))
    with pytest.raises(SdkError, match="initialize") as caught:
        sdk.join(_join(), Listener())
    assert "tok" not in str(caught.value)
    sdk, _ = _sdk(tmp_path / "b")
    FakeConnection.connect_result = -2
    try:
        with pytest.raises(SdkError, match="refused the join"):
            sdk.join(_join(), Listener())
    finally:
        FakeConnection.connect_result = 0


def test_the_callbacks_map_to_the_listener(tmp_path: Path) -> None:
    listener = Listener()
    sdk, _ = _sdk(tmp_path)
    sdk.join(_join(encryption=None), listener)
    conn = FakeConnection.last
    assert conn is not None
    connection, user, frames = conn.observers
    connection.on_connected(conn, None, 0)
    connection.on_user_joined(conn, "1")
    connection.on_user_joined(conn, "not-a-uid")
    connection.on_user_left(conn, "1", 0)
    connection.on_token_privilege_will_expire(conn, "old-token")
    connection.on_connection_failure(conn, None, 8)
    connection.on_token_privilege_did_expire(conn)
    connection.on_error(conn, 17, "message")
    # The wrapper compares this with 0 (a None raised the spike's TypeError).
    assert connection.on_aiqos_capability_missing(conn, 3) == -1
    user.on_user_video_track_state_changed(None, "2", None, 3, 0, 0)
    info = SimpleNamespace(codec_type=3, frame_type=3, capture_time_ms=1234)
    assert frames.on_encoded_video_frame(1, b"\0\0\0\1key", 7, info) == 1
    info = SimpleNamespace(codec_type=2, frame_type=4, capture_time_ms=0)
    frames.on_encoded_video_frame(2, b"\0\0\0\1d", 5, info)
    info = SimpleNamespace(codec_type=1, frame_type=3, capture_time_ms=0)
    frames.on_encoded_video_frame(2, b"vp8", 3, info)
    assert listener.calls == [
        ("connected",),
        ("joined", 1),
        ("left", 1),
        ("expire",),
        ("failed", "connection failure, reason 8"),
        ("failed", "the token expired"),
        ("warning", "sdk_error", "code 17"),
        ("state", 2, "frozen"),
        ("frame", 1, CODEC_H265, True, 1234, b"\0\0\0\1key"),
        ("frame", 2, CODEC_H264, False, 0, b"\0\0\0\1d"),
        ("frame", 2, 0, True, 0, b"vp8"),
    ]
    assert "old-token" not in repr(listener.calls)
