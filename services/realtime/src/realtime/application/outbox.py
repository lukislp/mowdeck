"""What a connection still has to send, and the policy when a client reads too slowly.

* ``state`` frames are snapshots: only the newest per mower matters. They sit in one slot per
  mower; a newer state replaces an unsent older one (counted as ``coalesced``).
* Everything else (``command_result``, ``subscribed``, ``error``, ...) goes through a bounded
  FIFO queue. When it is full the connection is a slow consumer and is closed with 4430.
* The writer always empties the queue before it sends states, so an answer such as
  ``subscribed`` is never overtaken by the state that follows it.
"""

import asyncio
from collections import OrderedDict, deque
from collections.abc import Iterable
from dataclasses import dataclass
from uuid import UUID

from mowdeck_contracts.v1.realtime import RealtimeCloseCode

from realtime.domain.models import CloseRequest


@dataclass(frozen=True, slots=True)
class Frame:
    """An encoded message and its type (for the metrics)."""

    type: str
    text: str


class Outbox:
    """Pending frames of one connection; never blocks the producer."""

    def __init__(self, queue_size: int) -> None:
        self.queue_size = queue_size
        self._queue: deque[Frame] = deque()
        self._states: OrderedDict[UUID, Frame] = OrderedDict()
        self._wakeup = asyncio.Event()
        self.close_request: CloseRequest | None = None
        self.coalesced = 0

    @property
    def closing(self) -> bool:
        """True once a close was requested; nothing more is accepted."""
        return self.close_request is not None

    def offer(self, frame: Frame) -> bool:
        """Queue a frame; False (and a 4430 close) when the queue is full."""
        if self.closing:
            return False
        if len(self._queue) >= self.queue_size:
            self.close(CloseRequest(RealtimeCloseCode.SLOW_CONSUMER, "slow consumer", flush=False))
            return False
        self._queue.append(frame)
        self._wakeup.set()
        return True

    def offer_state(self, mower_id: UUID, frame: Frame) -> bool:
        """Put the newest state of a mower; True when it replaced an unsent one."""
        if self.closing:
            return False
        replaced = self._states.pop(mower_id, None) is not None
        self._states[mower_id] = frame
        if replaced:
            self.coalesced += 1
        self._wakeup.set()
        return replaced

    def discard_states(self, mower_ids: Iterable[UUID]) -> None:
        """Forget unsent states (after an unsubscribe or a revocation)."""
        for mower_id in mower_ids:
            self._states.pop(mower_id, None)

    def close(self, request: CloseRequest) -> None:
        """Ask the writer to finish; the first request wins."""
        if self.close_request is None:
            self.close_request = request
        self._wakeup.set()

    def next_frame(self) -> Frame | None:
        """The next frame to send: queued messages first, then the states in arrival order."""
        if self._queue:
            return self._queue.popleft()
        if self._states:
            _, frame = self._states.popitem(last=False)
            return frame
        return None

    def drain_queue(self) -> list[Frame]:
        """Take every queued (non-state) frame, used to flush before a close."""
        frames = list(self._queue)
        self._queue.clear()
        return frames

    async def wait(self) -> None:
        """Wait until something can be sent or a close was requested."""
        if self._queue or self._states or self.closing:
            return
        self._wakeup.clear()
        await self._wakeup.wait()

    def pending(self) -> int:
        """Frames waiting (queue plus state slots)."""
        return len(self._queue) + len(self._states)
