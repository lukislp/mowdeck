# SPDX-License-Identifier: Apache-2.0
"""The bridge's control handling, records, exit codes and back-pressure with a fake SDK."""

import json
import logging
import queue
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any

import pytest

from agora_bridge.bridge import (
    EXIT_BAD_INPUT,
    EXIT_FAILURE,
    EXIT_JOIN_FAILED,
    EXIT_OK,
    Bridge,
)
from agora_bridge.outbox import Outbox
from agora_bridge.protocol import CODEC_H265, Frame, Join, RecordReader
from agora_bridge.sdk import SdkError, SdkListener

TOKEN = "007eJx-test-token-SECRET"  # noqa: S105 - a test value
KEY = "encryption-key-SECRET"
SALT = "c2FsdHNhbHRzYWx0c2FsdHNhbHRzYWx0c2FsdHNhbHQ="
APP_ID = "app-id-SECRET"
CHANNEL = "channel-SECRET"
SECRETS = (TOKEN, KEY, SALT, APP_ID, CHANNEL)
KEYFRAME = b"\x00\x00\x00\x01\x40\x01" + b"k" * 50
DELTA = b"\x00\x00\x00\x01\x02\x01" + b"d" * 20


def join_line(**extra: Any) -> str:
    data = {
        "op": "join",
        "app_id": APP_ID,
        "channel": CHANNEL,
        "token": TOKEN,
        "uid": 4242,
        "encryption": {"mode": "aes-256-gcm2", "key": KEY, "salt": SALT},
        **extra,
    }
    return json.dumps(data) + "\n"


@dataclass
class FakeSdk:
    """Records the calls; the test drives the callbacks through ``listener``."""

    version: str = "fake-sdk/1"
    fail_join: bool = False
    renew_result: int = 0
    joins: list[Join] = field(default_factory=list)
    subscribed: list[int] = field(default_factory=list)
    keyframes: list[int] = field(default_factory=list)
    renewed: list[str] = field(default_factory=list)
    left: int = 0
    listener: SdkListener | None = None
    joined_event: threading.Event = field(default_factory=threading.Event)

    def join(self, join: Join, listener: SdkListener) -> None:
        if self.fail_join:
            msg = "the SDK refused the join: -2"
            raise SdkError(msg)
        self.joins.append(join)
        self.listener = listener
        self.joined_event.set()

    def subscribe(self, uid: int) -> int:
        self.subscribed.append(uid)
        return 0

    def keyframe(self, uid: int) -> int:
        self.keyframes.append(uid)
        return 0

    def renew(self, token: str) -> int:
        self.renewed.append(token)
        return self.renew_result

    def leave(self) -> None:
        self.left += 1


class Sink:
    """A thread-safe stdout that collects the records."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._reader = RecordReader()
        self.records: list[Frame | dict[str, Any]] = []
        self.raw = bytearray()
        self.blocked = threading.Event()
        self.blocked.set()
        self.fail = False

    def write(self, data: bytes) -> int:
        self.blocked.wait(10)
        if self.fail:
            raise BrokenPipeError
        with self._lock:
            self.raw += data
            self.records += self._reader.feed(bytes(data))
        return len(data)

    def flush(self) -> None:
        return None

    def events(self, name: str | None = None) -> list[dict[str, Any]]:
        with self._lock:
            return [r for r in self.records if isinstance(r, dict) and name in (None, r["event"])]

    def frames(self) -> list[Frame]:
        with self._lock:
            return [r for r in self.records if isinstance(r, Frame)]


class Stdin:
    """Lines the test sends; ``close`` is the end of the input."""

    def __init__(self) -> None:
        self._lines: queue.Queue[str | None] = queue.Queue()

    def send(self, line: str) -> None:
        self._lines.put(line)

    def close(self) -> None:
        self._lines.put(None)

    def __iter__(self) -> Iterator[str]:
        while (line := self._lines.get()) is not None:
            yield line


@dataclass
class Run:
    bridge: Bridge
    sdk: FakeSdk
    sink: Sink
    stdin: Stdin
    thread: threading.Thread
    result: list[int]

    def wait(self, timeout: float = 5.0) -> int:
        self.thread.join(timeout)
        assert not self.thread.is_alive(), "the bridge did not exit"
        return self.result[0]

    def until(self, check: Any, timeout: float = 3.0) -> None:
        deadline = time.monotonic() + timeout
        while not check():
            assert time.monotonic() < deadline, "condition not reached"
            time.sleep(0.01)

    def connect(self) -> SdkListener:
        self.stdin.send(join_line())
        assert self.sdk.joined_event.wait(3)
        assert self.sdk.listener is not None
        self.sdk.listener.connected()
        self.until(lambda: self.sink.events("joined"))
        return self.sdk.listener


def start(sdk: FakeSdk | None = None, **kwargs: Any) -> Run:
    sdk = sdk or FakeSdk()
    sink, stdin, result = Sink(), Stdin(), []
    ns = iter(range(1_000_000_000, 10**12, 100_000_000))
    bridge = Bridge(sdk, sink, monotonic_ns=lambda: next(ns), **kwargs)  # type: ignore[arg-type]
    thread = threading.Thread(target=lambda: result.append(bridge.run(stdin)), daemon=True)
    thread.start()
    return Run(bridge, sdk, sink, stdin, thread, result)


def test_hello_comes_first_and_join_subscribes_only_the_listed_publishers() -> None:
    run = start()
    listener = run.connect()
    assert run.sink.events()[0] == {
        "event": "hello",
        "protocol": 1,
        "bridge": "agora-bridge/0.1.0",
        "sdk": "fake-sdk/1",
    }
    join = run.sdk.joins[0]
    assert (join.app_id, join.channel, join.token, join.uid) == (APP_ID, CHANNEL, TOKEN, 4242)
    assert join.encryption is not None and join.encryption.salt == SALT
    for uid in (1, 2, 3):
        listener.user_joined(uid)
    run.stdin.send('{"op": "subscribe", "uids": [1, 2]}\n')
    run.until(lambda: sorted(run.sdk.subscribed) == [1, 2])
    run.stdin.send('{"op": "subscribe", "uids": [3, 7]}\n')  # 7 joins later
    run.until(lambda: sorted(run.sdk.subscribed) == [1, 2, 3])
    listener.user_joined(7)
    run.until(lambda: sorted(run.sdk.subscribed) == [1, 2, 3, 7])
    published = [e["uid"] for e in run.sink.events("publisher") if e["state"] == "published"]
    assert published == [1, 2, 3, 7]
    run.stdin.send('{"op": "leave"}\n')
    assert run.wait() == EXIT_OK
    assert run.sdk.left == 1
    assert run.sink.events()[-1] == {"event": "left"}


def test_a_subscription_before_the_join_waits_for_the_connection() -> None:
    run = start()
    run.stdin.send('{"op": "subscribe", "uids": [1]}\n')
    run.stdin.send(join_line())
    assert run.sdk.joined_event.wait(3)
    assert run.sdk.listener is not None
    run.sdk.listener.user_joined(1)
    time.sleep(0.1)
    assert run.sdk.subscribed == []  # not connected yet
    run.sdk.listener.connected()
    run.until(lambda: run.sdk.subscribed == [1])
    run.stdin.close()
    assert run.wait() == EXIT_OK


def test_a_publisher_that_leaves_and_returns_is_subscribed_again() -> None:
    run = start()
    listener = run.connect()
    run.stdin.send('{"op": "subscribe", "uids": [1]}\n')
    listener.user_joined(1)
    run.until(lambda: run.sdk.subscribed == [1])
    listener.user_left(1)
    listener.user_joined(1)
    run.until(lambda: run.sdk.subscribed == [1, 1])
    states = [e["state"] for e in run.sink.events("publisher")]
    assert states == ["published", "unpublished", "published"]
    run.stdin.close()
    assert run.wait() == EXIT_OK


def test_frames_become_records_with_flags_and_receive_time() -> None:
    run = start()
    listener = run.connect()
    listener.frame(1, CODEC_H265, True, 9_000_100, KEYFRAME)
    listener.frame(1, CODEC_H265, False, 0, DELTA)
    run.until(lambda: len(run.sink.frames()) == 2)
    key, delta = run.sink.frames()
    assert (key.uid, key.codec, key.keyframe, key.capture_ts, key.data) == (
        1, CODEC_H265, True, 9_000_100, KEYFRAME,
    )  # fmt: skip
    assert (delta.keyframe, delta.capture_ts) == (False, None)
    assert delta.recv_mono_ns > key.recv_mono_ns
    run.stdin.close()
    assert run.wait() == EXIT_OK


def test_unsupported_frames_are_reported_once_and_dropped() -> None:
    run = start()
    listener = run.connect()
    listener.frame(1, 0, True, 0, KEYFRAME)
    listener.frame(1, 0, True, 0, KEYFRAME)
    listener.frame(2, CODEC_H265, True, 0, b"\x00\x00\x00\x10avcc")
    run.stdin.close()
    assert run.wait() == EXIT_OK
    codes = [e["code"] for e in run.sink.events("error")]
    assert codes == ["unsupported_codec", "not_annexb"]
    assert run.sink.frames() == []


def test_sdk_events_are_forwarded() -> None:
    run = start()
    listener = run.connect()
    listener.video_state(1, "decoding")
    listener.token_will_expire()
    listener.warning("sdk_error", "code 17")
    run.stdin.close()
    assert run.wait() == EXIT_OK
    assert {"event": "video_state", "uid": 1, "state": "decoding"} in run.sink.events()
    assert run.sink.events("token_will_expire")
    assert {"event": "error", "code": "sdk_error", "detail": "code 17"} in run.sink.events()


def test_renew_reports_its_outcome() -> None:
    run = start()
    run.stdin.send('{"op": "renew", "token": "early"}\n')
    run.until(lambda: run.sink.events("renew_failed"))
    assert run.sink.events("renew_failed")[0]["reason"] == "not_joined"
    run.connect()
    run.stdin.send('{"op": "renew", "token": "fresh-1", "uid": 99}\n')
    run.until(lambda: run.sink.events("renewed"))
    run.sdk.renew_result = -7
    run.stdin.send('{"op": "renew", "token": "fresh-2"}\n')
    run.until(lambda: len(run.sink.events("renew_failed")) == 2)
    assert run.sink.events("renew_failed")[1]["reason"] == "sdk_error_-7"
    assert run.sdk.renewed == ["fresh-1", "fresh-2"]
    run.stdin.close()
    assert run.wait() == EXIT_OK


def test_keyframe_requests_reach_the_sdk_and_unknown_ops_are_answered() -> None:
    run = start()
    run.connect()
    run.stdin.send('{"op": "keyframe", "uid": 2}\n')
    run.stdin.send('{"op": "rewind"}\n')
    run.until(lambda: run.sink.events("error"))
    assert run.sink.events("error")[0]["code"] == "unknown_op"
    run.until(lambda: run.sdk.keyframes == [2])
    run.stdin.close()
    assert run.wait() == EXIT_OK


@pytest.mark.parametrize("line", ["garbage\n", '{"op": "join", "uid": 1}\n'])
def test_bad_input_exits_with_2(line: str) -> None:
    run = start()
    run.stdin.send(line)
    assert run.wait() == EXIT_BAD_INPUT
    assert run.sink.events("error")[0]["code"] == "bad_input"


def test_a_second_join_is_bad_input() -> None:
    run = start()
    run.connect()
    run.stdin.send(join_line())
    assert run.wait() == EXIT_BAD_INPUT
    assert run.sdk.left == 1


def test_a_refused_join_exits_with_4() -> None:
    run = start(FakeSdk(fail_join=True))
    run.stdin.send(join_line())
    assert run.wait() == EXIT_JOIN_FAILED
    error = run.sink.events("error")[0]
    assert error["code"] == "join_failed"


def test_a_failed_connection_before_joining_exits_with_4_and_after_with_1() -> None:
    run = start()
    run.stdin.send(join_line())
    assert run.sdk.joined_event.wait(3)
    assert run.sdk.listener is not None
    run.sdk.listener.connection_failed("connection failure, reason 8")
    assert run.wait() == EXIT_JOIN_FAILED
    run = start()
    run.connect().connection_failed("the token expired")
    assert run.wait() == EXIT_FAILURE
    assert run.sink.events("error")[-1]["code"] == "connection_failed"


def test_a_join_without_connection_times_out() -> None:
    run = start(join_timeout=0.2)
    run.stdin.send(join_line())
    assert run.wait() == EXIT_JOIN_FAILED
    assert run.sink.events("error")[0]["detail"] == "no connection in time"


def test_a_closed_stdout_ends_the_bridge_with_1() -> None:
    run = start()
    run.connect()
    run.sink.fail = True
    assert run.sdk.listener is not None
    run.sdk.listener.frame(1, CODEC_H265, True, 0, KEYFRAME)
    assert run.wait() == EXIT_FAILURE
    assert run.sdk.left == 1


def test_no_secret_reaches_the_records_or_the_log(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    run = start()
    listener = run.connect()
    run.stdin.send('{"op": "subscribe", "uids": [1]}\n')
    listener.user_joined(1)
    run.stdin.send(json.dumps({"op": "renew", "token": TOKEN + "-2", "uid": 5}) + "\n")
    run.until(lambda: run.sink.events("renewed"))
    run.stdin.send('{"op": "leave"}\n')
    assert run.wait() == EXIT_OK
    for secret in SECRETS:
        assert secret.encode() not in bytes(run.sink.raw)
        assert secret not in caplog.text


# --- Back-pressure ----------------------------------------------------------------------------


def _frame(uid: int, key: bool, n: int) -> Frame:
    return Frame(uid, CODEC_H265, key, n, None, KEYFRAME if key else DELTA)


def test_a_full_queue_drops_deltas_until_the_next_key_frame_and_reports_the_count() -> None:
    sink = Sink()
    sink.blocked.clear()  # the relay does not read
    outbox = Outbox(sink, max_frames=3)  # type: ignore[arg-type]
    outbox.event("hello")
    accepted = [outbox.frame(_frame(1, i == 0, i)) for i in range(4)]
    # The writer is stuck on the hello; three frames fit; the fourth is dropped.
    assert accepted == [True, True, True, False]
    assert outbox.dropped(1) == 1
    assert outbox.frame(_frame(2, True, 10)) is False  # still full, another uid
    sink.blocked.set()  # the relay reads again
    time.sleep(0.1)
    assert outbox.frame(_frame(1, False, 11)) is False  # a delta after a gap is useless
    assert outbox.frame(_frame(2, False, 12)) is False
    assert outbox.frame(_frame(1, True, 13)) is True  # recovered on a key frame
    outbox.close()
    states = sink.events("video_state")
    assert states == [{"event": "video_state", "uid": 1, "state": "decoding", "dropped": 2}]
    assert [f.recv_mono_ns for f in sink.frames()] == [0, 1, 2, 13]


def test_events_are_never_dropped_and_keep_their_order() -> None:
    sink = Sink()
    sink.blocked.clear()
    outbox = Outbox(sink, max_frames=1)  # type: ignore[arg-type]
    outbox.frame(_frame(1, True, 0))
    outbox.frame(_frame(1, True, 1))
    for i in range(10):
        outbox.event("publisher", uid=i, state="published")
    sink.blocked.set()
    outbox.close()
    assert [e["uid"] for e in sink.events("publisher")] == list(range(10))


def test_a_frame_beyond_the_record_limit_is_dropped_until_the_next_key_frame() -> None:
    sink = Sink()
    outbox = Outbox(sink)  # type: ignore[arg-type]
    huge = Frame(1, CODEC_H265, True, 0, None, KEYFRAME + bytes(5 * 1024 * 1024))
    assert outbox.frame(huge) is False  # never raises into the SDK's callback thread
    assert outbox.frame(_frame(1, False, 1)) is False
    assert outbox.frame(_frame(1, True, 2)) is True
    outbox.close()
    assert [f.recv_mono_ns for f in sink.frames()] == [2]
