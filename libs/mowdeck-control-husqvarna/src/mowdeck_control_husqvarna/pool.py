"""One shared :class:`~mowdeck_control_husqvarna.account.HusqvarnaAccount` per application.

The rate limits of the Automower Connect API count per application key, and one websocket
already carries the events of every mower of the account. So the discovery and the connections
of all mowers of one linked account share one account session: :class:`AccountPool` starts it
once, hands out reference counted :class:`AccountLease` objects and stops it when the last lease
is released (after ``linger_seconds``, so a discovery followed by the registration of the listed
mowers needs one login, not two).

Sessions are keyed by the application key (the account's public identity). Which secret a
session belongs to is decided by comparing the secret itself (``hmac.compare_digest``, constant
time) with the one the session holds for its login: a changed secret opens a new session, no
hash or other derivative of a secret is computed or kept, and neither value is logged.
"""

import asyncio
import contextlib
import hmac
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Final

from mowdeck_control import CloudUnavailable

from mowdeck_control_husqvarna.account import HusqvarnaAccount

_log = logging.getLogger("mowdeck_control_husqvarna.pool")

DEFAULT_LINGER_SECONDS: Final = 120.0
"""How long an unused session stays open (discovery, then registering the listed mowers)."""

AccountOpener = Callable[[str, str], Awaitable[HusqvarnaAccount]]
"""``open(app_key, app_secret)``: a started account; raises ``CloudLoginRefused`` or
``CloudUnavailable``."""


def same_secret(a: str, b: str) -> bool:
    """Constant-time comparison of two secrets (nothing is derived from them)."""
    return hmac.compare_digest(a.encode("utf-8"), b.encode("utf-8"))


class PoolClosedError(CloudUnavailable):
    """The pool was closed (the gateway shuts down)."""


@dataclass(eq=False, slots=True)
class _Entry:
    key: str
    secret: str = field(repr=False)
    account: HusqvarnaAccount
    refs: int = 0
    linger: asyncio.Task[None] | None = field(default=None, repr=False)


class AccountLease:
    """A share of one pooled account session; :meth:`release` once (idempotent)."""

    def __init__(self, pool: "AccountPool", entry: _Entry, *, reused: bool) -> None:
        self._pool = pool
        self._entry: _Entry | None = entry
        self.account = entry.account
        self.reused = reused
        """True when the session was already open (no login for this lease)."""

    async def release(self) -> None:
        """Give the share back; the last one stops the session (after the linger time)."""
        entry, self._entry = self._entry, None
        if entry is not None:
            await self._pool._release(entry)


class AccountPool:
    """Reference counted account sessions, one per application key and secret."""

    def __init__(
        self, opener: AccountOpener, *, linger_seconds: float = DEFAULT_LINGER_SECONDS
    ) -> None:
        self._opener = opener
        self._linger_seconds = linger_seconds
        self._entries: dict[str, list[_Entry]] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._closed = False
        self.logins = 0
        """Sessions started so far (tests and metrics)."""

    @property
    def open_sessions(self) -> int:
        """Sessions currently held (in use or lingering)."""
        return sum(len(entries) for entries in self._entries.values())

    def _find(self, app_key: str, app_secret: str) -> _Entry | None:
        for entry in self._entries.get(app_key.strip(), []):
            if same_secret(entry.secret, app_secret):
                return entry
        return None

    def _held(self, entry: _Entry) -> bool:
        return entry in self._entries.get(entry.key, [])

    def _drop(self, entry: _Entry) -> None:
        entries = self._entries.get(entry.key, [])
        if entry in entries:
            entries.remove(entry)
        if not entries:
            self._entries.pop(entry.key, None)

    def users(self, app_key: str, app_secret: str) -> int:
        """Leases on the session of these credentials (0 when none is open)."""
        entry = self._find(app_key, app_secret)
        return entry.refs if entry is not None else 0

    async def acquire(self, app_key: str, app_secret: str) -> AccountLease:
        """A lease on the session of these credentials, starting one when none is open.
        Concurrent acquires wait for one start; a failed start leaves nothing behind."""
        if self._closed:
            raise PoolClosedError("the Husqvarna session pool is closed")
        key = app_key.strip()
        lock = self._locks.setdefault(key, asyncio.Lock())
        async with lock:
            entry = self._find(app_key, app_secret)
            if entry is not None and not entry.account.alive:
                self._drop(entry)
                entry = None
            reused = entry is not None
            if entry is None:
                account = await self._opener(app_key, app_secret)
                if self._closed:
                    await account.stop()
                    raise PoolClosedError("the Husqvarna session pool is closed")
                self.logins += 1
                entry = _Entry(key, app_secret, account)
                self._entries.setdefault(key, []).append(entry)
            if entry.linger is not None:
                entry.linger.cancel()
                entry.linger = None
            entry.refs += 1
        return AccountLease(self, entry, reused=reused)

    async def _release(self, entry: _Entry) -> None:
        entry.refs = max(0, entry.refs - 1)
        if entry.refs > 0:
            return
        if not self._held(entry) or self._linger_seconds <= 0:
            await self._stop(entry)
            return
        entry.linger = asyncio.get_running_loop().create_task(self._expire(entry))

    async def _expire(self, entry: _Entry) -> None:
        await asyncio.sleep(self._linger_seconds)
        if entry.refs > 0 or not self._held(entry):
            return
        entry.linger = None
        await self._stop(entry)

    async def _stop(self, entry: _Entry) -> None:
        self._drop(entry)
        try:
            await entry.account.stop()
        except Exception:
            _log.exception("stopping a Husqvarna session failed")

    async def close(self) -> None:
        """Stop every session (shutdown); later acquires raise :class:`PoolClosedError`."""
        self._closed = True
        entries = [e for held in self._entries.values() for e in held]
        for entry in entries:
            if entry.linger is not None:
                entry.linger.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await entry.linger
                entry.linger = None
            await self._stop(entry)
        self._entries.clear()
        self._locks.clear()
