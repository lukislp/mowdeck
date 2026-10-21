"""Shared helpers of the fuzz targets (standard library only).

Every target is a module ``fuzz_<name>.py`` next to this file with

* ``test_one_input(data: bytes) -> None``: one input; it returns normally for every input the
  code under test handles as specified and raises (an ``AssertionError`` or the unexpected
  exception) otherwise, which the fuzzer reports as a crash;
* ``main()``: the Atheris entry point ClusterFuzzLite builds into a fuzzer binary.

The targets read typed values from the fuzz bytes with :class:`Input` rather than Atheris's
``FuzzedDataProvider``, so ``tools/repo-checks/tests/test_fuzz_targets.py`` replays them under
pytest without Atheris installed.
"""

import math
import struct
from collections.abc import Coroutine, Sequence
from typing import Any

_DOUBLE = struct.Struct(">d")


class Input:
    """Reads typed values from the front of the fuzz bytes; past the end it yields zeros."""

    def __init__(self, data: bytes) -> None:
        self._data = data
        self._pos = 0

    @property
    def remaining(self) -> int:
        """Bytes not consumed yet."""
        return len(self._data) - self._pos

    def take(self, count: int) -> bytes:
        """The next ``count`` bytes (fewer at the end)."""
        chunk = self._data[self._pos : self._pos + max(0, count)]
        self._pos += len(chunk)
        return chunk

    def rest(self) -> bytes:
        """Everything not consumed yet."""
        return self.take(self.remaining)

    def uint(self, size: int) -> int:
        """An unsigned big-endian integer of ``size`` bytes."""
        return int.from_bytes(self.take(size).ljust(size, b"\0"), "big")

    def int_in(self, low: int, high: int) -> int:
        """An integer in ``[low, high]``."""
        span = high - low
        if span <= 0:
            return low
        size = max(1, (span.bit_length() + 7) // 8)
        return low + self.uint(size) % (span + 1)

    def boolean(self) -> bool:
        """One bit of the next byte."""
        return bool(self.uint(1) & 1)

    def double(self) -> float:
        """Any IEEE double, NaN and the infinities included."""
        return float(_DOUBLE.unpack(self.take(8).ljust(8, b"\0"))[0])

    def float_in(self, low: float, high: float) -> float:
        """A finite float in ``[low, high]``."""
        return low + (high - low) * (self.uint(4) / 0xFFFFFFFF)

    def number(self) -> float:
        """A float that is often an edge case: 0, +-1, huge, tiny, NaN, the infinities."""
        special = (0.0, -0.0, 1.0, -1.0, 1e308, -1e308, 5e-324, math.inf, -math.inf, math.nan)
        choice = self.uint(1)
        if choice < len(special):
            return special[choice]
        if choice < 128:
            return self.float_in(-1000.0, 1000.0)
        return self.double()

    def text(self, max_bytes: int) -> str:
        """A string of up to ``max_bytes`` UTF-8 bytes (invalid sequences replaced)."""
        size = self.int_in(0, max_bytes)
        return self.take(size).decode("utf-8", "replace")

    def pick[T](self, choices: Sequence[T]) -> T:
        """One of ``choices``."""
        return choices[self.int_in(0, len(choices) - 1)]


def run_sync[T](coroutine: Coroutine[Any, Any, T]) -> T:
    """Run a coroutine that never suspends (no event loop per input)."""
    try:
        coroutine.send(None)
    except StopIteration as done:
        return done.value  # type: ignore[no-any-return]
    coroutine.close()
    msg = "the coroutine suspended"
    raise AssertionError(msg)
