"""Injectable timers, so the dead-man switch can be tested without sleeping.

Production code uses :class:`AsyncioTimerFactory`; tests use
:class:`mowdeck_control.testing.ManualTimers`, which fires timers when the test advances time.
"""

import asyncio
import logging
from collections.abc import Callable, Coroutine
from typing import Any, Protocol

TimerCallback = Callable[[], Coroutine[Any, Any, None]]

_log = logging.getLogger("mowdeck_control.timers")


class Timer(Protocol):
    """A scheduled callback that can be cancelled."""

    def cancel(self) -> None:
        """Cancel the timer; a no-op if it already fired."""
        ...


class TimerFactory(Protocol):
    """Schedules ``callback`` to run once after ``delay_s`` seconds."""

    def __call__(self, delay_s: float, callback: TimerCallback) -> Timer:
        """Start a timer."""
        ...


class _AsyncioTimer:
    def __init__(self, handle: asyncio.TimerHandle) -> None:
        self._handle = handle

    def cancel(self) -> None:
        self._handle.cancel()


class AsyncioTimerFactory:
    """Timers on the running asyncio event loop (``loop.call_later``)."""

    def __init__(self) -> None:
        self._tasks: set[asyncio.Task[None]] = set()

    def __call__(self, delay_s: float, callback: TimerCallback) -> Timer:
        loop = asyncio.get_running_loop()

        def fire() -> None:
            task = loop.create_task(callback())
            self._tasks.add(task)
            task.add_done_callback(self._done)

        return _AsyncioTimer(loop.call_later(delay_s, fire))

    def _done(self, task: asyncio.Task[None]) -> None:
        self._tasks.discard(task)
        if not task.cancelled() and task.exception() is not None:
            _log.error("timer callback failed", exc_info=task.exception())
