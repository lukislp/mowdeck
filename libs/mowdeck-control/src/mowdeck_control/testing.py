"""Deterministic time for tests: a manual clock and manual timers.

Typical use::

    clock = ManualClock()
    timers = ManualTimers(clock)
    controller = MowerController(connection, mower_id=..., clock=clock, timer_factory=timers)
    ...
    await timers.advance(1.5)  # fires the dead-man timer, no sleeping
"""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from mowdeck_control.timers import Timer, TimerCallback

DEFAULT_START = datetime(2026, 10, 8, 12, 0, tzinfo=UTC)


class ManualClock:
    """A clock that only moves when told to. Call it to get the current time."""

    def __init__(self, start: datetime = DEFAULT_START) -> None:
        if start.tzinfo is None:
            msg = "start must be timezone-aware"
            raise ValueError(msg)
        self.now = start

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> datetime:
        """Move the clock forward by ``seconds`` and return the new time."""
        self.now += timedelta(seconds=seconds)
        return self.now


@dataclass(eq=False)
class _ManualTimer:
    due: datetime
    callback: TimerCallback
    cancelled: bool = False

    def cancel(self) -> None:
        self.cancelled = True


class ManualTimers:
    """A :class:`~mowdeck_control.timers.TimerFactory` driven by a :class:`ManualClock`."""

    def __init__(self, clock: ManualClock) -> None:
        self.clock = clock
        self._timers: list[_ManualTimer] = []

    def __call__(self, delay_s: float, callback: TimerCallback) -> Timer:
        timer = _ManualTimer(self.clock() + timedelta(seconds=delay_s), callback)
        self._timers.append(timer)
        return timer

    @property
    def pending(self) -> int:
        """Number of timers that are neither fired nor cancelled."""
        return sum(1 for t in self._timers if not t.cancelled)

    async def advance(self, seconds: float) -> int:
        """Advance the clock and run every due callback in due order. Returns how many fired."""
        target = self.clock.advance(seconds)
        fired = 0
        while True:
            due = [t for t in self._timers if not t.cancelled and t.due <= target]
            if not due:
                break
            timer = min(due, key=lambda t: t.due)
            self._timers.remove(timer)
            await timer.callback()
            fired += 1
        self._timers = [t for t in self._timers if not t.cancelled]
        return fired
