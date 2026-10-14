"""Fan out state snapshots to any number of async subscribers.

Adapters use :class:`StateBroadcaster` to implement ``watch_state()``; the controller uses it to
re-publish states. Each subscriber has a bounded queue: a slow subscriber loses the oldest
snapshots, never blocks the publisher, and always ends up with the newest one.
"""

import asyncio
from collections.abc import AsyncIterator
from typing import Final

from mowdeck_contracts.v1 import MowerState

_CLOSED: Final = object()


class StateSubscription(AsyncIterator[MowerState]):
    """An async iterator over published states. Registered on creation, so no update published
    after :meth:`StateBroadcaster.subscribe` returns is missed."""

    def __init__(self, owner: "StateBroadcaster", maxsize: int) -> None:
        self._owner = owner
        self._queue: asyncio.Queue[object] = asyncio.Queue(maxsize=maxsize)
        self._done = False

    def _offer(self, item: object) -> None:
        if self._queue.full():
            self._queue.get_nowait()  # drop the oldest snapshot
        self._queue.put_nowait(item)

    def __aiter__(self) -> "StateSubscription":
        return self

    async def __anext__(self) -> MowerState:
        if self._done:
            raise StopAsyncIteration
        item = await self._queue.get()
        if item is _CLOSED:
            self._done = True
            raise StopAsyncIteration
        assert isinstance(item, MowerState)  # noqa: S101 - internal invariant
        return item

    async def aclose(self) -> None:
        """Stop receiving states."""
        self._done = True
        self._owner._discard(self)


class StateBroadcaster:
    """Publishes :class:`MowerState` snapshots to subscribers."""

    def __init__(self, maxsize: int = 32) -> None:
        if maxsize < 1:
            msg = "maxsize must be at least 1"
            raise ValueError(msg)
        self._maxsize = maxsize
        self._subscribers: list[StateSubscription] = []

    @property
    def subscriber_count(self) -> int:
        """Number of active subscriptions."""
        return len(self._subscribers)

    def subscribe(self, initial: MowerState | None = None) -> StateSubscription:
        """Return a new subscription; ``initial`` (if given) is delivered first."""
        subscription = StateSubscription(self, self._maxsize)
        if initial is not None:
            subscription._offer(initial)
        self._subscribers.append(subscription)
        return subscription

    def publish(self, state: MowerState) -> None:
        """Deliver ``state`` to every subscriber without waiting."""
        for subscription in self._subscribers:
            subscription._offer(state)

    def close(self) -> None:
        """End all current subscriptions. New subscriptions can still be made afterwards."""
        subscribers, self._subscribers = self._subscribers, []
        for subscription in subscribers:
            subscription._offer(_CLOSED)

    def _discard(self, subscription: StateSubscription) -> None:
        if subscription in self._subscribers:
            self._subscribers.remove(subscription)
