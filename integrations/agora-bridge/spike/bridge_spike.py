# SPDX-License-Identifier: Apache-2.0
"""Feasibility spike for the agora-bridge (ADR 0045 draft, step 0).

Joins Mammotion's Agora channel with the Agora Linux Server SDK (``agora-python-server-sdk``)
as a receive-only audience member, subscribes the mower's cameras with ``encodedFrameOnly``
and records what arrives, without decoding anything:

* every connection / user / subscription event (``events.jsonl``),
* one row per encoded frame (``frames.csv``: receive time, uid, codec, frame type, size,
  width, height, fps, capture time, NAL unit types),
* the raw access units of one uid as an elementary stream (the replay fixture),
* CPU and RSS of this process every few seconds.

Optional probes: an intra (key frame) request, a token renew with a second session read from
stdin, and a re-join some time after leaving (does the mower still publish?).

The session (``CameraSession`` JSON as Fleet gets it) is read from stdin, one JSON object per
line. Token, app id and encryption key stay in memory; nothing secret is written or logged.
"""

import argparse
import csv
import json
import os
import queue
import resource
import sys
import threading
import time
from pathlib import Path

from agora.rtc.agora_base import (
    AgoraServiceConfig,
    ChannelProfileType,
    ClientRoleType,
    EncryptionConfig,
    RTCConnConfig,
    RtcConnectionPublishConfig,
    VideoStreamType,
    VideoSubscriptionOptions,
)
from agora.rtc.agora_service import AgoraService
from agora.rtc.local_user_observer import IRTCLocalUserObserver
from agora.rtc.rtc_connection import RTCConnection
from agora.rtc.rtc_connection_observer import IRTCConnectionObserver
from agora.rtc.video_encoded_frame_observer import IVideoEncodedFrameObserver

T0 = time.monotonic()
CLK_TCK = os.sysconf("SC_CLK_TCK")
AGORA_GCM2_MODES = {"aes-128-gcm2": 7, "aes-256-gcm2": 8}


def now_s() -> float:
    return round(time.monotonic() - T0, 3)


class Recorder:
    """Thread-safe sink for events and frames (SDK callbacks come from native threads)."""

    def __init__(
        self,
        out: Path,
        fixture_uid: int,
        fixture_path: Path,
        fixture_seconds: float,
        fixture_max_bytes: int,
    ) -> None:
        self.lock = threading.Lock()
        self.events = (out / "events.jsonl").open("w", buffering=1)
        self.frames_file = (out / "frames.csv").open("w", newline="", buffering=1)
        self.frames = csv.writer(self.frames_file)
        self.frames.writerow(
            [
                "t",
                "wall_ms",
                "uid",
                "codec",
                "frame_type",
                "bytes",
                "width",
                "height",
                "fps",
                "rotation",
                "capture_time_ms",
                "decode_time_ms",
                "stream_type",
                "annexb",
                "nal_types",
            ]
        )
        self.fixture_uid = fixture_uid
        self.fixture = fixture_path.open("wb")
        self.fixture_seconds = fixture_seconds
        self.fixture_max_bytes = fixture_max_bytes
        self.fixture_started: float | None = None
        self.fixture_bytes = 0
        self.fixture_done = False
        self.counts: dict[int, int] = {}
        self.first_key_after: dict[int, float] = {}

    def event(self, kind: str, **data) -> None:
        line = json.dumps({"t": now_s(), "event": kind, **data}, default=str)
        with self.lock:
            self.events.write(line + "\n")
        print(line, file=sys.stderr, flush=True)

    def frame(self, uid: int, buf: bytes, info) -> None:
        annexb = buf[:4] == b"\x00\x00\x00\x01" or buf[:3] == b"\x00\x00\x01"
        nals = nal_types_h265(buf) if annexb and info.codec_type == 3 else []
        t = now_s()
        with self.lock:
            self.counts[uid] = self.counts.get(uid, 0) + 1
            self.frames.writerow(
                [
                    t,
                    int(time.time() * 1000),
                    uid,
                    info.codec_type,
                    info.frame_type,
                    len(buf),
                    info.width,
                    info.height,
                    info.frames_per_second,
                    info.rotation,
                    info.capture_time_ms,
                    info.decode_time_ms,
                    info.stream_type,
                    int(annexb),
                    ".".join(str(n) for n in nals),
                ]
            )
            if uid == self.fixture_uid and not self.fixture_done:
                is_key = info.frame_type == 3
                if self.fixture_started is None:
                    if not is_key:
                        return  # start the fixture on a key frame
                    self.fixture_started = t
                if (
                    t - self.fixture_started > self.fixture_seconds
                    or self.fixture_bytes + len(buf) > self.fixture_max_bytes
                ):
                    self.fixture_done = True
                    self.fixture.close()
                    return
                self.fixture.write(buf)
                self.fixture_bytes += len(buf)

    def close(self) -> None:
        with self.lock:
            if not self.fixture_done:
                self.fixture.close()
            self.events.close()
            self.frames_file.close()


def nal_types_h265(buf: bytes) -> list[int]:
    """H.265 NAL unit types of an Annex-B access unit (32 VPS, 33 SPS, 34 PPS, 19/20 IDR,
    21 CRA, 1 TRAIL_R, 39/40 SEI)."""
    types = []
    i = 0
    n = len(buf)
    while i < n - 3:
        if buf[i] == 0 and buf[i + 1] == 0 and buf[i + 2] == 1:
            if i + 3 < n:
                types.append((buf[i + 3] >> 1) & 0x3F)
            i += 3
        else:
            i += 1
    return types


class ConnObserver(IRTCConnectionObserver):
    def __init__(self, rec: Recorder, tasks: "queue.Queue[tuple]") -> None:
        self.rec = rec
        self.tasks = tasks

    def on_connected(self, conn, info, reason):
        self.rec.event("connected", state=info.state, local_uid=_uid(info), reason=reason)

    def on_disconnected(self, conn, info, reason):
        self.rec.event("disconnected", state=info.state, reason=reason)

    def on_connecting(self, conn, info, reason):
        self.rec.event("connecting", state=info.state, reason=reason)

    def on_reconnecting(self, conn, info, reason):
        self.rec.event("reconnecting", state=info.state, reason=reason)

    def on_reconnected(self, conn, info, reason):
        self.rec.event("reconnected", state=info.state, reason=reason)

    def on_connection_lost(self, conn, info):
        self.rec.event("connection_lost", state=info.state)

    def on_connection_failure(self, conn, info, reason):
        self.rec.event("connection_failure", state=info.state, reason=reason)

    def on_token_privilege_will_expire(self, conn, token):
        self.rec.event("token_will_expire")

    def on_token_privilege_did_expire(self, conn):
        self.rec.event("token_did_expire")

    def on_user_joined(self, conn, user_id):
        self.rec.event("user_joined", uid=user_id)
        self.tasks.put(("joined", str(user_id)))

    def on_user_left(self, conn, user_id, reason):
        self.rec.event("user_left", uid=user_id, reason=reason)

    def on_error(self, conn, error_code, error_msg):
        self.rec.event("error", code=error_code, msg=error_msg)

    def on_warning(self, conn, warn_code, warn_msg):
        self.rec.event("warning", code=warn_code, msg=warn_msg)

    def on_change_role_success(self, conn, old_role, new_role):
        self.rec.event("role", old=old_role, new=new_role)

    def on_change_role_failure(self, conn, reason, cur_role):
        self.rec.event("role_failure", reason=reason, role=cur_role)


def _uid(info) -> str | None:
    return getattr(info, "local_user_id", None)


class UserObserver(IRTCLocalUserObserver):
    def __init__(self, rec: Recorder) -> None:
        self.rec = rec

    def on_user_video_track_subscribed(self, local_user, user_id, info, track):
        self.rec.event("video_subscribed", uid=user_id, codec=getattr(info, "codec_type", None))

    def on_user_video_track_state_changed(self, local_user, user_id, track, state, reason, elapsed):
        self.rec.event("video_state", uid=user_id, state=state, reason=reason, elapsed=elapsed)

    def on_video_subscribe_state_changed(self, local_user, channel, user_id, old, new, elapsed):
        self.rec.event("video_subscribe_state", uid=user_id, old=old, new=new)

    def on_first_remote_video_frame(self, local_user, user_id, width, height, elapsed):
        self.rec.event(
            "first_video_frame", uid=user_id, width=width, height=height, elapsed=elapsed
        )

    def on_video_size_changed(self, local_user, user_id, width, height, rotation):
        self.rec.event("video_size", uid=user_id, width=width, height=height, rotation=rotation)

    def on_user_state_changed(self, local_user, user_id, state):
        self.rec.event("user_state", uid=user_id, state=state)

    def on_remote_video_track_statistics(self, local_user, track, stats):
        pass


class FrameObserver(IVideoEncodedFrameObserver):
    def __init__(self, rec: Recorder) -> None:
        self.rec = rec

    def on_encoded_video_frame(self, uid, image_buffer, length, info):
        self.rec.frame(int(uid), image_buffer, info)
        return 1


def proc_usage() -> dict:
    with open("/proc/self/stat") as f:
        parts = f.read().rsplit(")", 1)[1].split()
    utime, stime = int(parts[11]), int(parts[12])
    rss_kb = 0
    threads = 0
    with open("/proc/self/status") as f:
        for line in f:
            if line.startswith("VmRSS:"):
                rss_kb = int(line.split()[1])
            elif line.startswith("Threads:"):
                threads = int(line.split()[1])
    return {
        "cpu_s": (utime + stime) / CLK_TCK,
        "rss_mb": round(rss_kb / 1024, 1),
        "threads": threads,
        "max_rss_mb": round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024, 1),
    }


def read_session(stream) -> dict | None:
    line = stream.readline()
    if not line:
        return None
    return json.loads(line)


class Join:
    """One connection to the channel."""

    def __init__(self, service: AgoraService, session: dict, rec: Recorder, args) -> None:
        self.rec = rec
        self.tasks: queue.Queue[tuple] = queue.Queue()
        profile = (
            ChannelProfileType.CHANNEL_PROFILE_COMMUNICATION
            if args.profile == "rtc"
            else ChannelProfileType.CHANNEL_PROFILE_LIVE_BROADCASTING
        )
        conn_config = RTCConnConfig(
            auto_subscribe_audio=0,
            auto_subscribe_video=0,
            client_role_type=ClientRoleType.CLIENT_ROLE_AUDIENCE,
            channel_profile=profile,
        )
        publish = RtcConnectionPublishConfig(is_publish_audio=False, is_publish_video=False)
        self.conn = RTCConnection(service, conn_config, publish)
        self.conn.register_observer(ConnObserver(rec, self.tasks))
        self.conn.register_local_user_observer(UserObserver(rec))
        self.conn.register_video_encoded_frame_observer(FrameObserver(rec))
        enc = session.get("encryption")
        if enc:
            mode = AGORA_GCM2_MODES[enc["mode"]]
            salt = None
            if enc.get("salt"):
                import base64

                salt = bytearray(base64.b64decode(enc["salt"]))
            ret = self.conn.enable_encryption(
                1,
                EncryptionConfig(
                    encryption_mode=mode, encryption_key=enc["key"], encryption_kdf_salt=salt
                ),
            )
            rec.event("encryption", mode=enc["mode"], ret=ret)
        self.wanted = {str(u) for u in args.uids}
        self.subscribed: set[str] = set()
        self.local_user = self.conn.get_local_user()
        t = time.monotonic()
        ret = self.conn.connect(session["token"], session["channel"], str(session["uid"]))
        rec.event("connect_called", ret=ret, call_ms=round((time.monotonic() - t) * 1000, 1))

    def subscribe(self, uid: str) -> None:
        if uid in self.subscribed or (self.wanted and uid not in self.wanted):
            return
        ret = self.local_user.subscribe_video(
            uid,
            VideoSubscriptionOptions(type=VideoStreamType.VIDEO_STREAM_HIGH, encodedFrameOnly=True),
        )
        self.subscribed.add(uid)
        self.rec.event("subscribe_video", uid=uid, ret=ret)

    def pump(self) -> None:
        while True:
            try:
                kind, uid = self.tasks.get_nowait()
            except queue.Empty:
                return
            if kind == "joined":
                self.subscribe(uid)

    def leave(self) -> None:
        t = time.monotonic()
        ret = self.conn.disconnect()
        self.rec.event(
            "disconnect_called", ret=ret, call_ms=round((time.monotonic() - t) * 1000, 1)
        )
        self.conn.release()


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--out", type=Path, required=True)
    p.add_argument("--duration", type=float, default=90.0)
    p.add_argument("--uids", type=int, nargs="*", default=[1, 2, 3])
    p.add_argument("--profile", choices=["rtc", "live"], default="rtc")
    p.add_argument("--fixture-uid", type=int, default=1)
    p.add_argument("--fixture-seconds", type=float, default=25.0)
    p.add_argument("--fixture-max-bytes", type=int, default=4_800_000)
    p.add_argument("--intra-at", type=float, default=40.0, help="send an intra request (s)")
    p.add_argument(
        "--renew",
        action="store_true",
        help="read a second session line from stdin and renew the token with it",
    )
    p.add_argument(
        "--rejoin-after",
        type=float,
        default=0.0,
        help="seconds after leaving to join again with the same token (0 = no)",
    )
    p.add_argument("--rejoin-duration", type=float, default=20.0)
    p.add_argument("--sdk-log", type=Path, default=Path("/tmp/agora-sdk-log"))
    args = p.parse_args()

    args.out.mkdir(parents=True, exist_ok=True)
    args.sdk_log.mkdir(parents=True, exist_ok=True)
    rec = Recorder(
        args.out,
        args.fixture_uid,
        args.out / f"luba-uid{args.fixture_uid}.h265",
        args.fixture_seconds,
        args.fixture_max_bytes,
    )

    session = read_session(sys.stdin)
    if session is None:
        sys.exit("no session on stdin")
    rec.event(
        "session",
        channel_len=len(session["channel"]),
        viewer_uid=session["uid"],
        cameras=session.get("cameras"),
        encrypted=session.get("encryption") is not None,
        expires_at=session.get("expires_at"),
        provider=session.get("provider"),
    )

    renew_lines: queue.Queue[dict] = queue.Queue()
    if args.renew:

        def reader() -> None:
            s = read_session(sys.stdin)
            if s is not None:
                renew_lines.put(s)

        threading.Thread(target=reader, daemon=True).start()

    rec.event("usage", phase="before_init", **proc_usage())
    config = AgoraServiceConfig()
    config.appid = session["app_id"]
    config.enable_video = 1
    config.enable_audio_processor = 0
    config.enable_audio_device = 0
    config.log_path = str(args.sdk_log / "agorasdk.log")
    config.config_dir = str(args.sdk_log)
    config.data_dir = str(args.sdk_log)
    config.channel_profile = (
        ChannelProfileType.CHANNEL_PROFILE_COMMUNICATION
        if args.profile == "rtc"
        else ChannelProfileType.CHANNEL_PROFILE_LIVE_BROADCASTING
    )
    service = AgoraService()
    ret = service.initialize(config)
    rec.event("service_initialized", ret=ret)
    rec.event("usage", phase="after_init", **proc_usage())

    join = Join(service, session, rec, args)
    intra_done = False
    last_usage = 0.0
    start = time.monotonic()
    cpu_start = proc_usage()["cpu_s"]
    while time.monotonic() - start < args.duration:
        join.pump()
        el = time.monotonic() - start
        if not intra_done and args.intra_at and el >= args.intra_at:
            uid = str(args.fixture_uid)
            r = join.local_user.send_intra_request(uid)
            rec.event("intra_request", uid=uid, ret=r)
            intra_done = True
        try:
            fresh = renew_lines.get_nowait()
        except queue.Empty:
            fresh = None
        if fresh is not None:
            same = (
                fresh["channel"] == session["channel"],
                fresh["uid"] == session["uid"],
                fresh["app_id"] == session["app_id"],
            )
            r = join.conn.renew_token(fresh["token"])
            rec.event(
                "renew_token",
                ret=r,
                same_channel=same[0],
                same_uid=same[1],
                same_app=same[2],
                expires_at=fresh.get("expires_at"),
            )
        if el - last_usage >= 5:
            u = proc_usage()
            rec.event("usage", phase="joined", counts=dict(rec.counts), **u)
            last_usage = el
        time.sleep(0.05)
    u = proc_usage()
    rec.event(
        "usage_summary",
        wall_s=round(time.monotonic() - start, 1),
        cpu_s=round(u["cpu_s"] - cpu_start, 2),
        cpu_pct=round(100 * (u["cpu_s"] - cpu_start) / (time.monotonic() - start), 1),
        rss_mb=u["rss_mb"],
        max_rss_mb=u["max_rss_mb"],
        counts=dict(rec.counts),
    )
    join.leave()

    if args.rejoin_after > 0:
        rec.event("waiting_before_rejoin", seconds=args.rejoin_after)
        time.sleep(args.rejoin_after)
        before = dict(rec.counts)
        join2 = Join(service, session, rec, args)
        t = time.monotonic()
        while time.monotonic() - t < args.rejoin_duration:
            join2.pump()
            time.sleep(0.05)
        delta = {k: v - before.get(k, 0) for k, v in rec.counts.items()}
        rec.event("rejoin_summary", frames=delta)
        join2.leave()

    service.release()
    rec.event("done")
    rec.close()


if __name__ == "__main__":
    main()
