"""In-process token buckets (per client IP) for the login and token endpoints.

Per IP limits live in memory: they protect one instance against a burst and need no shared
state. With several instances, Traefik's ``rateLimit`` middleware is the shared outer limit.
The per username limit (failures in a time window) lives in the database (``login_attempts``),
so it holds across instances and restarts.
"""

import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass


@dataclass(slots=True)
class _Bucket:
    tokens: float
    updated: float


class TokenBucketLimiter:
    """``capacity`` requests at once, refilled at ``per_minute`` per key; LRU bounded."""

    def __init__(
        self,
        per_minute: float,
        capacity: int,
        *,
        max_keys: int = 10_000,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if per_minute <= 0 or capacity <= 0:
            msg = "per_minute and capacity must be positive"
            raise ValueError(msg)
        self.rate = per_minute / 60.0
        self.capacity = float(capacity)
        self.max_keys = max_keys
        self._clock = clock
        self._buckets: OrderedDict[str, _Bucket] = OrderedDict()

    def take(self, key: str) -> float:
        """Consume one token. Returns 0 if allowed, else the seconds until one is available."""
        now = self._clock()
        bucket = self._buckets.get(key)
        if bucket is None:
            bucket = _Bucket(self.capacity, now)
            self._buckets[key] = bucket
        else:
            bucket.tokens = min(self.capacity, bucket.tokens + (now - bucket.updated) * self.rate)
            bucket.updated = now
        self._buckets.move_to_end(key)
        while len(self._buckets) > self.max_keys:
            self._buckets.popitem(last=False)
        if bucket.tokens >= 1.0:
            bucket.tokens -= 1.0
            return 0.0
        return (1.0 - bucket.tokens) / self.rate
