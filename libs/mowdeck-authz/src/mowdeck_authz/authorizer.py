"""The one question: may this subject do this on that object?

Every implementation answers it with a plain ``bool`` and **fails closed**: when the answer
cannot be determined (OpenFGA down, timeout, malformed response) the answer is ``False``.
"""

import json
import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable, Mapping, Sequence
from typing import Any, NamedTuple, Protocol, runtime_checkable

from mowdeck_authz.refs import ObjectRef, Subject

Context = Mapping[str, Any]
"""Request context for conditions, e.g. ``{"current_time": "2026-10-09T12:00:00Z"}``."""


class CheckItem(NamedTuple):
    """One question of a batch."""

    subject: Subject
    relation: str
    object: ObjectRef
    context: Context | None = None


CheckLike = CheckItem | tuple[Subject, str, ObjectRef]


def as_check_item(item: CheckLike) -> CheckItem:
    """Accept a :class:`CheckItem` or a plain ``(subject, relation, object)`` tuple."""
    return item if isinstance(item, CheckItem) else CheckItem(*item)


@runtime_checkable
class Authorizer(Protocol):
    """Answers authorization questions. Implementations never raise for a failed check."""

    async def check(
        self,
        subject: Subject,
        relation: str,
        obj: ObjectRef,
        *,
        context: Context | None = None,
    ) -> bool:
        """True only if ``subject`` has ``relation`` on ``obj``."""
        ...

    async def check_many(self, items: Sequence[CheckLike]) -> list[bool]:
        """Answer several questions at once; results are in the order of ``items``."""
        ...

    async def list_objects(
        self,
        subject: Subject,
        relation: str,
        object_type: str,
        *,
        context: Context | None = None,
    ) -> list[str]:
        """Objects (``"mower:<id>"``) of ``object_type`` on which ``subject`` has ``relation``."""
        ...


class SupportsPing(Protocol):
    """An authorizer that can report whether it is able to answer (used for readiness)."""

    async def ping(self) -> bool:
        """True if the backend is reachable and the store and model are resolved."""
        ...


def readiness_check_authz(authorizer: SupportsPing) -> Callable[[], Awaitable[bool]]:
    """A readiness check named ``authz`` for the service kit (``readiness_checks=[...]``)."""

    async def authz() -> bool:
        return await authorizer.ping()

    return authz


CacheKey = tuple[str, str, str, str]


def cache_key(item: CheckItem) -> CacheKey:
    """``(subject, relation, object, context)``; the context is serialised canonically."""
    context = json.dumps(dict(item.context), sort_keys=True, default=str) if item.context else ""
    return (str(item.subject), item.relation, str(item.object), context)


class DecisionCache:
    """Small in-process TTL and LRU cache of check results (positive and negative).

    A TTL of 0 disables it. ``clock`` is a monotonic clock in seconds (injectable for tests).
    """

    def __init__(
        self,
        ttl_seconds: float,
        max_entries: int = 10_000,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.ttl_seconds = ttl_seconds
        self.max_entries = max_entries
        self._clock = clock
        self._entries: OrderedDict[CacheKey, tuple[float, bool]] = OrderedDict()

    @property
    def enabled(self) -> bool:
        """False when the TTL is 0."""
        return self.ttl_seconds > 0 and self.max_entries > 0

    def __len__(self) -> int:
        return len(self._entries)

    def get(self, key: CacheKey) -> bool | None:
        """The cached decision, or None if absent or expired."""
        entry = self._entries.get(key)
        if entry is None:
            return None
        expires_at, allowed = entry
        if self._clock() >= expires_at:
            del self._entries[key]
            return None
        self._entries.move_to_end(key)
        return allowed

    def put(self, key: CacheKey, allowed: bool) -> None:
        """Store a decision for ``ttl_seconds``."""
        if not self.enabled:
            return
        self._entries[key] = (self._clock() + self.ttl_seconds, allowed)
        self._entries.move_to_end(key)
        while len(self._entries) > self.max_entries:
            self._entries.popitem(last=False)

    def invalidate(self) -> None:
        """Drop every entry (for example after a hierarchy change)."""
        self._entries.clear()

    def invalidate_subject(self, subject: Subject | str) -> None:
        """Drop the entries of one subject (after its grants changed)."""
        name = str(subject)
        prefix = f"{name}#"
        for key in [k for k in self._entries if k[0] == name or k[0].startswith(prefix)]:
            del self._entries[key]
