# SPDX-License-Identifier: Apache-2.0
"""The :class:`~agora_bridge.sdk.Sdk` over Agora's Linux server SDK (``agora-python-server-sdk``).

Facts from the spike against a LUBA 2 AWD X (ADR 0045) that shape this adapter:

* receive-only member: role audience, no audio, no auto-subscribe, profile ``rtc``
  (communication) unless the relay asks for ``live``; subscribed per uid with
  ``encodedFrameOnly`` (no decoding in the bridge); one connection receives every camera;
* frames are H.265 Annex-B with the parameter sets in each key frame; ``capture_time_ms`` is the
  sender's own clock; key-frame requests are accepted (return 0) but ignored by the mower;
* the wrapper raises a harmless ``TypeError`` in ``_on_capabilities_changed`` when the
  observer's ``on_aiqos_capability_missing`` returns None (``custome_specified >= 0``): the
  observer here returns -1 (keep the scenario);
* the SDK writes its log, config and data below a private temporary directory that is removed
  when the bridge exits.

Native callbacks run on the SDK's threads and only call the listener, which never blocks.
"""

import base64
import binascii
import logging
import shutil
import tempfile
from pathlib import Path
from types import ModuleType
from typing import Any

from agora_bridge.guard import WRAPPER_VERSION, import_agora
from agora_bridge.protocol import CODEC_H264, CODEC_H265, Join
from agora_bridge.sdk import SdkError, SdkListener

_log = logging.getLogger("agora_bridge.sdk")

ENCRYPTION_MODES = {
    "aes-128-xts": 1,
    "aes-128-ecb": 2,
    "aes-256-xts": 3,
    "sm4-128-ecb": 4,
    "aes-128-gcm": 5,
    "aes-256-gcm": 6,
    "aes-128-gcm2": 7,
    "aes-256-gcm2": 8,
}
"""Agora's ``EncryptionMode`` by the name the vendor session carries."""

_CODECS = {2: CODEC_H264, 3: CODEC_H265}
"""Agora ``VideoCodecType`` -> protocol codec."""
_KEY_FRAME = 3
"""Agora ``VIDEO_FRAME_TYPE_KEY_FRAME``."""
_VIDEO_STATES = {0: "stopped", 1: "starting", 2: "decoding", 3: "frozen", 4: "stopped"}
"""Agora ``REMOTE_VIDEO_STATE_*`` -> protocol state."""


def _uid(value: Any) -> int | None:
    try:
        uid = int(value)
    except (TypeError, ValueError):
        return None
    return uid if 0 <= uid <= 2**32 - 1 else None


def _observers(modules: dict[str, ModuleType], listener: SdkListener) -> tuple[Any, Any, Any]:
    """The connection, local user and encoded frame observers bound to ``listener``."""
    conn_base: Any = modules["rtc_connection_observer"].IRTCConnectionObserver
    user_base: Any = modules["local_user_observer"].IRTCLocalUserObserver
    frame_base: Any = modules["video_encoded_frame_observer"].IVideoEncodedFrameObserver

    class ConnectionObserver(conn_base):
        def on_connected(self, conn: Any, info: Any, reason: Any) -> None:
            listener.connected()

        def on_connection_failure(self, conn: Any, info: Any, reason: Any) -> None:
            listener.connection_failed(f"connection failure, reason {reason}")

        def on_connection_license_validation_failure(self, conn: Any, reason: Any) -> None:
            listener.connection_failed(f"licence validation failure, reason {reason}")

        def on_token_privilege_will_expire(self, conn: Any, token: Any) -> None:
            del token  # never touched
            listener.token_will_expire()

        def on_token_privilege_did_expire(self, conn: Any) -> None:
            listener.connection_failed("the token expired")

        def on_user_joined(self, conn: Any, user_id: Any) -> None:
            uid = _uid(user_id)
            if uid is not None:
                listener.user_joined(uid)

        def on_user_left(self, conn: Any, user_id: Any, reason: Any) -> None:
            uid = _uid(user_id)
            if uid is not None:
                listener.user_left(uid)

        def on_error(self, conn: Any, error_code: Any, error_msg: Any) -> None:
            listener.warning("sdk_error", f"code {error_code}")

        def on_encryption_error(self, conn: Any, error_type: Any) -> None:
            listener.warning("encryption_error", f"type {error_type}")

        def on_aiqos_capability_missing(self, conn: Any, recommend_audio_scenario: Any) -> int:
            # The wrapper compares this value with 0; None raised a TypeError (spike).
            return -1

    class UserObserver(user_base):
        def on_user_video_track_state_changed(
            self, local_user: Any, user_id: Any, track: Any, state: Any, reason: Any, elapsed: Any
        ) -> None:
            uid = _uid(user_id)
            if uid is not None:
                listener.video_state(uid, _VIDEO_STATES.get(int(state), "stopped"))

    class FrameObserver(frame_base):
        def on_encoded_video_frame(
            self, uid: Any, image_buffer: Any, length: Any, info: Any
        ) -> int:
            publisher = _uid(uid)
            if publisher is not None:
                listener.frame(
                    publisher,
                    _CODECS.get(int(info.codec_type), 0),
                    int(info.frame_type) == _KEY_FRAME,
                    int(info.capture_time_ms or 0),
                    bytes(image_buffer),
                )
            return 1

    return ConnectionObserver(), UserObserver(), FrameObserver()


class AgoraSdk:
    """One receive-only connection per process."""

    def __init__(self, modules: dict[str, ModuleType], workdir: Path) -> None:
        self._m = modules
        self._workdir = workdir
        self.version = f"agora-python-server-sdk/{WRAPPER_VERSION}"
        self._service: Any = None
        self._conn: Any = None
        self._user: Any = None
        self._observers: tuple[Any, Any, Any] | None = None

    @classmethod
    def load(cls) -> "AgoraSdk":
        """Import the wrapper offline (raises ``SdkLoadError``)."""
        modules = import_agora()
        return cls(modules, Path(tempfile.mkdtemp(prefix="agora-bridge-")))

    def join(self, join: Join, listener: SdkListener) -> None:
        base = self._m["agora_base"]
        rtc = base.ChannelProfileType.CHANNEL_PROFILE_COMMUNICATION
        live = base.ChannelProfileType.CHANNEL_PROFILE_LIVE_BROADCASTING
        profile = rtc if join.profile == "rtc" else live
        config = base.AgoraServiceConfig()
        config.appid = join.app_id
        config.enable_video = 1
        config.enable_audio_processor = 0
        config.enable_audio_device = 0
        config.log_path = str(self._workdir / "agorasdk.log")
        config.config_dir = str(self._workdir)
        config.data_dir = str(self._workdir)
        config.channel_profile = profile
        service = self._m["agora_service"].AgoraService()
        ret = service.initialize(config)
        if ret != 0:
            msg = f"the SDK did not initialize: {ret}"
            raise SdkError(msg)
        self._service = service
        conn_config = base.RTCConnConfig(
            auto_subscribe_audio=0,
            auto_subscribe_video=0,
            client_role_type=base.ClientRoleType.CLIENT_ROLE_AUDIENCE,
            channel_profile=profile,
        )
        publish = base.RtcConnectionPublishConfig(is_publish_audio=False, is_publish_video=False)
        conn = self._m["rtc_connection"].RTCConnection(service, conn_config, publish)
        if getattr(conn, "conn_handle", None) is None:
            msg = "the SDK refused to create a connection"
            raise SdkError(msg)
        self._conn = conn
        self._observers = _observers(self._m, listener)
        connection_observer, user_observer, frame_observer = self._observers
        conn.register_observer(connection_observer)
        conn.register_local_user_observer(user_observer)
        conn.register_video_encoded_frame_observer(frame_observer)
        if join.encryption is not None:
            self._encrypt(base, conn, join)
        self._user = conn.get_local_user()
        ret = conn.connect(join.token, join.channel, str(join.uid))
        if ret < 0:
            msg = f"the SDK refused the join: {ret}"
            raise SdkError(msg)

    @staticmethod
    def _encrypt(base: ModuleType, conn: Any, join: Join) -> None:
        assert join.encryption is not None  # noqa: S101 - checked by the caller
        mode = ENCRYPTION_MODES.get(join.encryption.mode.lower())
        if mode is None:
            msg = f"unsupported encryption mode {join.encryption.mode[:32]!r}"
            raise SdkError(msg)
        salt = None
        if join.encryption.salt:
            try:
                salt = bytearray(base64.b64decode(join.encryption.salt, validate=True))
            except (binascii.Error, ValueError) as exc:
                msg = "the encryption salt is not base64"
                raise SdkError(msg) from exc
        config = base.EncryptionConfig(
            encryption_mode=mode,
            encryption_key=join.encryption.key,
            encryption_kdf_salt=salt,
        )
        ret = conn.enable_encryption(1, config)
        if ret != 0:
            msg = f"the SDK refused the encryption config: {ret}"
            raise SdkError(msg)

    def subscribe(self, uid: int) -> int:
        if self._user is None:
            return -1
        options = self._m["agora_base"].VideoSubscriptionOptions(
            type=self._m["agora_base"].VideoStreamType.VIDEO_STREAM_HIGH, encodedFrameOnly=True
        )
        return int(self._user.subscribe_video(str(uid), options))

    def keyframe(self, uid: int) -> int:
        if self._user is None:
            return -1
        return int(self._user.send_intra_request(str(uid)))

    def renew(self, token: str) -> int:
        if self._conn is None:
            return -1
        return int(self._conn.renew_token(token))

    def leave(self) -> None:
        conn, service = self._conn, self._service
        self._conn = self._service = self._user = None
        try:
            if conn is not None:
                conn.disconnect()
                conn.release()
            if service is not None:
                service.release()
        except Exception:
            _log.exception("leaving the channel failed")
        finally:
            shutil.rmtree(self._workdir, ignore_errors=True)
