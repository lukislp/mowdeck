# SPDX-License-Identifier: Apache-2.0
"""The bounded output queue between the SDK's threads and stdout.

The SDK's callbacks never block: :meth:`Outbox.frame` copies the frame into a bounded queue and
returns. A writer thread writes the records to stdout; when the relay reads slowly, stdout
blocks the writer, the queue fills and frames are dropped (protocol "Behaviour"):

* a full queue drops the frame and every following delta frame of that uid until its next key
  frame fits (a decoder cannot use a delta frame after a gap);
* the count is reported in a ``video_state`` event (``state: decoding``, ``dropped``) when the
  uid recovers with a key frame.

Events are never dropped and keep their order with the frames.
"""

import threading
from collections import deque
from collections.abc import Callable
from typing import BinaryIO

from agora_bridge.protocol import Frame, ProtocolError, encode_event, encode_frame

DEFAULT_MAX_FRAMES = 64
"""About 3 s of two cameras at 10 fps."""
DEFAULT_MAX_BYTES = 8 * 1024 * 1024


class Outbox:
    """Thread-safe record queue with a writer thread."""

    def __init__(
        self,
        out: BinaryIO,
        *,
        max_frames: int = DEFAULT_MAX_FRAMES,
        max_bytes: int = DEFAULT_MAX_BYTES,
        on_failure: Callable[[], None] | None = None,
    ) -> None:
        self._out = out
        self._max_frames = max_frames
        self._max_bytes = max_bytes
        self._on_failure = on_failure
        self._lock = threading.Lock()
        self._ready = threading.Condition(self._lock)
        self._queue: deque[tuple[bytes, bool]] = deque()
        self._frames = 0
        self._bytes = 0
        self._awaiting_key: set[int] = set()
        self._dropped: dict[int, int] = {}
        self._closing = False
        self.failed = False
        self.written_frames = 0
        self._thread = threading.Thread(target=self._write_loop, name="bridge-writer", daemon=True)
        self._thread.start()

    def event(self, event: str, **fields: object) -> None:
        """Queue an event (never dropped)."""
        record = encode_event(event, **fields)
        with self._lock:
            if self._closing:
                return
            self._queue.append((record, False))
            self._ready.notify()

    def frame(self, frame: Frame) -> bool:
        """Queue a frame unless the queue is full or the uid waits for a key frame. Returns
        whether it was queued."""
        try:
            record = encode_frame(frame)
        except ProtocolError:
            record = b""  # beyond the record limit: dropped like a frame that does not fit
        with self._lock:
            if self._closing:
                return False
            uid = frame.uid
            if uid in self._awaiting_key and not frame.keyframe:
                self._dropped[uid] = self._dropped.get(uid, 0) + 1
                return False
            if (
                not record
                or self._frames >= self._max_frames
                or self._bytes + len(record) > self._max_bytes
            ):
                self._dropped[uid] = self._dropped.get(uid, 0) + 1
                self._awaiting_key.add(uid)
                return False
            self._queue.append((record, True))
            self._frames += 1
            self._bytes += len(record)
            if uid in self._awaiting_key:
                self._awaiting_key.discard(uid)
                dropped = self._dropped.pop(uid, 0)
                self._queue.append(
                    (encode_event("video_state", uid=uid, state="decoding", dropped=dropped), False)
                )
            self._ready.notify()
            return True

    def dropped(self, uid: int) -> int:
        """Frames of ``uid`` dropped and not yet reported."""
        with self._lock:
            return self._dropped.get(uid, 0)

    def close(self, timeout: float = 5.0) -> None:
        """Write what is queued (at most ``timeout`` seconds) and stop the writer."""
        with self._lock:
            self._closing = True
            self._ready.notify()
        self._thread.join(timeout)

    def _write_loop(self) -> None:
        while True:
            with self._lock:
                while not self._queue and not self._closing:
                    self._ready.wait()
                if not self._queue:
                    return
                record, is_frame = self._queue.popleft()
                if is_frame:
                    self._frames -= 1
                    self._bytes -= len(record)
                more = bool(self._queue)
            try:
                self._out.write(record)
                if not more:
                    self._out.flush()
            except (OSError, ValueError):
                with self._lock:
                    self.failed = True
                    self._closing = True
                    self._queue.clear()
                if self._on_failure is not None:
                    self._on_failure()
                return
            if is_frame:
                self.written_frames += 1
