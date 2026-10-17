# SPDX-License-Identifier: Apache-2.0
"""The bridge: control lines in, records out, one SDK connection in between.

:class:`Bridge` runs the protocol of docs/dev/camera-relay-bridge-protocol.md against an
:class:`~agora_bridge.sdk.Sdk`:

* a reader thread turns stdin into control lines on the main queue; the SDK's callbacks put
  what needs an SDK call (a joined user to subscribe, the connection state) on the same queue,
  so every SDK call runs on the main thread and never inside a callback;
* frames and plain events go straight to the :class:`~agora_bridge.outbox.Outbox`.

Exit codes: 0 after ``leave`` or closed stdin, 2 bad input, 3 SDK failed to load (raised before
the bridge runs), 4 join failed, 1 anything else.
"""

import contextlib
import logging
import queue
import threading
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import BinaryIO

from agora_bridge import __version__
from agora_bridge.outbox import DEFAULT_MAX_BYTES, DEFAULT_MAX_FRAMES, Outbox
from agora_bridge.protocol import (
    PROTOCOL_VERSION,
    BadInputError,
    Control,
    Frame,
    Join,
    Keyframe,
    Leave,
    Renew,
    Subscribe,
    UnknownOp,
    parse_control,
)
from agora_bridge.sdk import Sdk, SdkError

_log = logging.getLogger("agora_bridge")

EXIT_OK = 0
EXIT_FAILURE = 1
EXIT_BAD_INPUT = 2
EXIT_SDK_LOAD = 3
EXIT_JOIN_FAILED = 4

JOIN_TIMEOUT_SECONDS = 20.0
"""Without a connection this long after ``join``, the join failed."""


@dataclass(frozen=True, slots=True)
class _Line:
    text: str


@dataclass(frozen=True, slots=True)
class _Eof:
    pass


@dataclass(frozen=True, slots=True)
class _Connected:
    pass


@dataclass(frozen=True, slots=True)
class _ConnectionFailed:
    detail: str


@dataclass(frozen=True, slots=True)
class _User:
    uid: int
    joined: bool


@dataclass(frozen=True, slots=True)
class _OutputFailed:
    pass


_Item = _Line | _Eof | _Connected | _ConnectionFailed | _User | _OutputFailed


class _Listener:
    """The SDK's callbacks: frames and plain events to the outbox, the rest to the main queue."""

    def __init__(
        self, inbox: "queue.Queue[_Item]", outbox: Outbox, monotonic_ns: Callable[[], int]
    ) -> None:
        self._inbox = inbox
        self._outbox = outbox
        self._monotonic_ns = monotonic_ns
        self._unsupported: set[int] = set()

    def connected(self) -> None:
        self._inbox.put(_Connected())

    def connection_failed(self, detail: str) -> None:
        self._inbox.put(_ConnectionFailed(detail[:200]))

    def user_joined(self, uid: int) -> None:
        self._inbox.put(_User(uid, joined=True))

    def user_left(self, uid: int) -> None:
        self._inbox.put(_User(uid, joined=False))

    def video_state(self, uid: int, state: str) -> None:
        dropped = self._outbox.dropped(uid)
        if dropped:
            self._outbox.event("video_state", uid=uid, state=state, dropped=dropped)
        else:
            self._outbox.event("video_state", uid=uid, state=state)

    def token_will_expire(self) -> None:
        self._outbox.event("token_will_expire")

    def frame(self, uid: int, codec: int, keyframe: bool, capture_ms: int, data: bytes) -> None:
        recv = self._monotonic_ns()
        if codec == 0:
            if uid not in self._unsupported:
                self._unsupported.add(uid)
                self._outbox.event(
                    "error", code="unsupported_codec", detail=f"uid {uid}: not H.264 or H.265"
                )
            return
        if not data.startswith((b"\x00\x00\x01", b"\x00\x00\x00\x01")):
            if uid not in self._unsupported:
                self._unsupported.add(uid)
                self._outbox.event(
                    "error", code="not_annexb", detail=f"uid {uid}: frames are not Annex-B"
                )
            return
        self._outbox.frame(
            Frame(
                uid=uid,
                codec=codec,
                keyframe=keyframe,
                recv_mono_ns=recv,
                capture_ts=capture_ms if capture_ms > 0 else None,
                data=data,
            )
        )

    def warning(self, code: str, detail: str) -> None:
        self._outbox.event("error", code=code[:64], detail=detail[:200])


class Bridge:
    """One bridge process: one channel, any number of subscribed publishers."""

    def __init__(
        self,
        sdk: Sdk,
        out: BinaryIO,
        *,
        max_frames: int = DEFAULT_MAX_FRAMES,
        max_bytes: int = DEFAULT_MAX_BYTES,
        join_timeout: float = JOIN_TIMEOUT_SECONDS,
        monotonic: Callable[[], float] = time.monotonic,
        monotonic_ns: Callable[[], int] = time.monotonic_ns,
    ) -> None:
        self.sdk = sdk
        self._inbox: queue.Queue[_Item] = queue.Queue()
        self.outbox = Outbox(
            out,
            max_frames=max_frames,
            max_bytes=max_bytes,
            on_failure=lambda: self._inbox.put(_OutputFailed()),
        )
        self._listener = _Listener(self._inbox, self.outbox, monotonic_ns)
        self._join_timeout = join_timeout
        self._monotonic = monotonic
        self._join_started: float | None = None
        self._joined = False
        self._left = False
        self.wanted: set[int] = set()
        self.present: set[int] = set()
        self.subscribed: set[int] = set()

    # --- Running ------------------------------------------------------------------------------

    def run(self, lines: Iterable[str]) -> int:
        """Serve ``lines`` (stdin) until ``leave``, the end of the input or a failure; returns
        the exit code."""
        reader = threading.Thread(target=self._read, args=(lines,), name="bridge-stdin")
        reader.daemon = True
        reader.start()
        self.outbox.event(
            "hello",
            protocol=PROTOCOL_VERSION,
            bridge=f"agora-bridge/{__version__}",
            sdk=self.sdk.version,
        )
        code = EXIT_FAILURE
        try:
            code = self._loop()
        except Exception:
            _log.exception("bridge failed")
            self.outbox.event("error", code="internal", detail="the bridge failed")
            code = EXIT_FAILURE
        finally:
            self._leave()
            self.outbox.close()
        return code

    def _read(self, lines: Iterable[str]) -> None:
        try:
            for line in lines:
                if line.strip():
                    self._inbox.put(_Line(line))
        except (OSError, ValueError):
            _log.warning("stdin failed")
        self._inbox.put(_Eof())

    def _loop(self) -> int:
        while True:
            if (
                self._join_started is not None
                and not self._joined
                and self._monotonic() - self._join_started > self._join_timeout
            ):
                self.outbox.event("error", code="join_failed", detail="no connection in time")
                return EXIT_JOIN_FAILED
            try:
                item = self._inbox.get(timeout=0.25)
            except queue.Empty:
                continue
            code = self._handle(item)
            if code is not None:
                return code

    def _handle(self, item: _Item) -> int | None:
        match item:
            case _Line(text):
                try:
                    control = parse_control(text)
                except BadInputError as exc:
                    self.outbox.event("error", code="bad_input", detail=str(exc))
                    return EXIT_BAD_INPUT
                return self._control(control)
            case _Eof():
                _log.info("stdin closed: leaving")
                self._leave()
                return EXIT_OK
            case _Connected():
                if not self._joined and self._join_started is not None:
                    self._joined = True
                    elapsed = int((self._monotonic() - self._join_started) * 1000)
                    self.outbox.event("joined", elapsed_ms=elapsed)
                    _log.info("joined in %d ms", elapsed)
                    self._subscribe_wanted()
                return None
            case _ConnectionFailed(detail):
                fatal_code = EXIT_FAILURE if self._joined else EXIT_JOIN_FAILED
                code = "connection_failed" if self._joined else "join_failed"
                self.outbox.event("error", code=code, detail=detail)
                return fatal_code
            case _User(uid, joined):
                self._user(uid, joined)
            case _OutputFailed():
                _log.warning("stdout failed: the relay is gone")
                return EXIT_FAILURE
        return None

    def _control(self, control: Control) -> int | None:
        match control:
            case Join():
                if self._join_started is not None:
                    self.outbox.event("error", code="bad_input", detail="join sent twice")
                    return EXIT_BAD_INPUT
                self._join_started = self._monotonic()
                try:
                    self.sdk.join(control, self._listener)
                except SdkError as exc:
                    self.outbox.event("error", code="join_failed", detail=str(exc)[:200])
                    return EXIT_JOIN_FAILED
                _log.info("joining (uid set, encryption %s)", control.encryption is not None)
            case Renew():
                self._renew(control)
            case Subscribe(uids):
                self.wanted.update(uids)  # additive (protocol): no unsubscribe in version 1
                self._subscribe_wanted()
            case Keyframe(uid):
                if self._joined:
                    ret = self.sdk.keyframe(uid)
                    _log.info("key frame request for uid %d: %d", uid, ret)
            case Leave():
                self._leave()
                return EXIT_OK
            case UnknownOp(op):
                self.outbox.event("error", code="unknown_op", detail=f"unknown op {op!r}")
        return None

    def _renew(self, renew: Renew) -> None:
        if not self._joined:
            self.outbox.event("renew_failed", reason="not_joined")
            return
        ret = self.sdk.renew(renew.token)
        if ret == 0:
            self.outbox.event("renewed")
            _log.info("token renewed (uid %s)", "given" if renew.uid is not None else "unchanged")
        else:
            self.outbox.event("renew_failed", reason=f"sdk_error_{ret}")
            _log.warning("token renew refused: %d", ret)

    def _user(self, uid: int, joined: bool) -> None:
        if joined:
            self.present.add(uid)
            self.outbox.event("publisher", uid=uid, state="published")
            self._subscribe_wanted()
        else:
            self.present.discard(uid)
            self.subscribed.discard(uid)
            self.outbox.event("publisher", uid=uid, state="unpublished")

    def _subscribe_wanted(self) -> None:
        if not self._joined:
            return
        for uid in sorted((self.wanted & self.present) - self.subscribed):
            ret = self.sdk.subscribe(uid)
            if ret == 0:
                self.subscribed.add(uid)
            else:
                self.outbox.event("error", code="subscribe_failed", detail=f"uid {uid}: {ret}")
            _log.info("subscribed uid %d: %d", uid, ret)

    def _leave(self) -> None:
        if self._left:
            return
        self._left = True
        with contextlib.suppress(Exception):
            self.sdk.leave()  # also releases what the SDK set up without a join
        self.outbox.event("left")
