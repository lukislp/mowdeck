"""Which accounts somebody watches (public demo sandboxes, ADR 0076).

Realtime announces the live sockets per account (``presence.accounts``,
:class:`~mowdeck_contracts.v1.AccountPresence`). In a demo installation the Device-Gateway pauses
the simulated mowers of an account nobody watches: no simulation ticks, no heartbeats, so an idle
sandbox costs nothing but its database rows. An account is awake while

* a Realtime instance reported at least one socket for it, and that report is younger than
  ``grace`` keep-alive intervals (an instance that died stops counting by itself), or
* a request reached one of its mowers less than ``wake_seconds`` ago (a command over the API, a
  scheduled start, a photo): the mower answers it like an awake one and then rests again.

Pure bookkeeping with an injected monotonic clock; the gateway applies the answers to its
sessions.
"""

import time
from collections.abc import Callable, Iterable
from uuid import UUID

from mowdeck_contracts.v1 import AccountPresence

DEFAULT_GRACE = 2.5
"""Keep-alive intervals after which an instance's report no longer counts."""


class PresenceTracker:
    """Watchers per account and Realtime instance, and short wake-ups by requests."""

    def __init__(
        self,
        *,
        wake_seconds: float = 120.0,
        grace: float = DEFAULT_GRACE,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._wake_seconds = wake_seconds
        self._grace = grace
        self._clock = clock
        self._watch: dict[UUID, dict[str, tuple[int, float]]] = {}
        """account -> instance -> (watchers, valid until)."""
        self._woken: dict[UUID, float] = {}
        """account -> awake until (a request)."""
        self._awake: set[UUID] = set()
        """The accounts last answered awake (to tell what changed)."""

    def awake(self, account_id: UUID) -> bool:
        """Whether the mowers of ``account_id`` should run now."""
        return self._compute(account_id, self._clock())

    def awake_accounts(self) -> frozenset[UUID]:
        """The accounts last answered awake."""
        return frozenset(self._awake)

    def update(self, presence: AccountPresence) -> set[UUID]:
        """Take one announcement; returns the accounts whose state changed."""
        now = self._clock()
        until = now + presence.interval_seconds * self._grace
        listed = {entry.account_id for entry in presence.accounts}
        touched = set(listed)
        for entry in presence.accounts:
            self._watch.setdefault(entry.account_id, {})[presence.instance] = (
                entry.watchers,
                until,
            )
        if presence.full:  # the instance watches nothing else
            for account_id, instances in self._watch.items():
                if account_id not in listed and presence.instance in instances:
                    del instances[presence.instance]
                    touched.add(account_id)
        return self._refresh(touched, now)

    def wake(self, account_id: UUID) -> set[UUID]:
        """A request reached a mower of ``account_id``; returns it when it just woke up."""
        now = self._clock()
        self._woken[account_id] = max(self._woken.get(account_id, 0.0), now + self._wake_seconds)
        return self._refresh((account_id,), now)

    def expire(self) -> set[UUID]:
        """Forget what timed out; returns the accounts that fell asleep."""
        now = self._clock()
        for account_id in list(self._watch):
            instances = self._watch[account_id]
            for instance in [i for i, (_, until) in instances.items() if until <= now]:
                del instances[instance]
            if not instances:
                del self._watch[account_id]
        for account_id in [a for a, until in self._woken.items() if until <= now]:
            del self._woken[account_id]
        return self._refresh(set(self._awake), now)

    def forget(self, account_id: UUID) -> None:
        """Drop everything about ``account_id`` (its last mower was removed)."""
        self._watch.pop(account_id, None)
        self._woken.pop(account_id, None)
        self._awake.discard(account_id)

    def _compute(self, account_id: UUID, now: float) -> bool:
        if self._woken.get(account_id, 0.0) > now:
            return True
        return any(
            watchers > 0 and until > now
            for watchers, until in self._watch.get(account_id, {}).values()
        )

    def _refresh(self, accounts: Iterable[UUID], now: float) -> set[UUID]:
        changed: set[UUID] = set()
        for account_id in accounts:
            awake = self._compute(account_id, now)
            if awake != (account_id in self._awake):
                changed.add(account_id)
                if awake:
                    self._awake.add(account_id)
                else:
                    self._awake.discard(account_id)
        return changed
