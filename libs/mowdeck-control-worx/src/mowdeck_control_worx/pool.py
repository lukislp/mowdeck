"""One shared :class:`~mowdeck_control_worx.session.WorxSession` per account, and a login guard.

The vendor throttles logins and has blocked accounts that logged in too often (ADR 0066). So
the discovery of a linked account and the connections of all its mowers share one session (one
login, one MQTT link): :class:`AccountPool` opens it once, hands out reference counted
:class:`AccountLease` objects and stops it when the last lease is released (after
``linger_seconds``, so a discovery followed by the registration of the listed mowers needs one
login, not two).

The gateway reconnects a failed mower connection every few seconds (up to 30 s apart); without a
guard every attempt would be a login. :class:`LoginGuard` therefore remembers per account:

* a **refused** login (wrong password) for :data:`REFUSED_HOLD_S`: the same credentials are
  refused again without asking the cloud (a corrected password is another key and is tried);
* a **rate limited** login (HTTP 429) for :data:`RATE_LIMIT_HOLD_S`, and any other failed login
  for :data:`UNAVAILABLE_HOLD_S`;
* every successful login, so the next one with the same credentials waits at least
  :data:`MIN_LOGIN_INTERVAL_S` (a session that died a moment ago is not replaced at once; a
  corrected or changed password is tried at once).

The pool key is an HMAC (random key of this process) of brand, e-mail and password: a changed
password opens a new session, and no credential is kept in the key or logged.
"""

import asyncio
import contextlib
import hashlib
import hmac
import logging
import math
import os
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Final

from mowdeck_control import CloudLoginRefused, CloudUnavailable

from mowdeck_control_worx.session import (
    LoginRateLimited,
    SessionOpener,
    WorxLogin,
    WorxSession,
)

_log = logging.getLogger("mowdeck_control_worx.pool")

DEFAULT_LINGER_SECONDS: Final = 120.0
"""How long an unused session stays open (discovery, then registering the listed mowers)."""
REFUSED_HOLD_S: Final = 15 * 60.0
"""A refused login is not tried again with the same credentials for this long."""
RATE_LIMIT_HOLD_S: Final = 60 * 60.0
"""After HTTP 429 the account is not logged in again for this long."""
UNAVAILABLE_HOLD_S: Final = 2 * 60.0
"""After a cloud error the account is not logged in again for this long."""
MIN_LOGIN_INTERVAL_S: Final = 60.0
"""Shortest time between two logins of the same account."""

_PROCESS_KEY: Final = os.urandom(32)

PoolKey = tuple[str, str]
"""(account tag, credentials tag): the first names the account, the second its password."""


def pool_key(login: WorxLogin) -> PoolKey:
    """The pool key of a login (no credential is kept in it)."""

    def tag(value: str) -> str:
        return hmac.new(_PROCESS_KEY, value.encode("utf-8"), hashlib.sha256).hexdigest()

    account = tag(f"account:{login.brand}:{login.email.strip().casefold()}")
    return account, tag(f"password:{account}:{login.password}")


class PoolClosedError(CloudUnavailable):
    """The pool was closed (the gateway shuts down)."""


def _minutes(seconds: float) -> int:
    return max(1, math.ceil(seconds / 60))


@dataclass(slots=True)
class LoginGuard:
    """Refuses logins the cloud should not see now (see the module docstring)."""

    clock: Callable[[], float] = time.monotonic
    _held: dict[PoolKey, tuple[float, Exception]] = field(default_factory=dict)
    _last_login: dict[PoolKey, float] = field(default_factory=dict)

    def check(self, key: PoolKey) -> None:
        """Raise the held failure, or :class:`CloudUnavailable` while the account logged in a
        moment ago; return when a login may go out."""
        now = self.clock()
        held = self._held.get(key)
        if held is not None:
            until, error = held
            if now < until:
                wait = _minutes(until - now)
                if isinstance(error, CloudLoginRefused):
                    msg = f"{error} (not tried again for {wait} min)"
                    raise CloudLoginRefused(msg)
                msg = f"{error} (MowDeck waits {wait} min before the next login)"
                raise CloudUnavailable(msg)
            del self._held[key]
        last = self._last_login.get(key)
        if last is not None and now - last < MIN_LOGIN_INTERVAL_S:
            wait_s = math.ceil(MIN_LOGIN_INTERVAL_S - (now - last))
            msg = f"the account logged in a moment ago; MowDeck waits {wait_s} s to log in again"
            raise CloudUnavailable(msg)

    def failed(self, key: PoolKey, error: Exception) -> None:
        """The login failed with ``error``: hold the credentials for a while."""
        if isinstance(error, CloudLoginRefused):
            hold = REFUSED_HOLD_S
        elif isinstance(error, LoginRateLimited):
            hold = RATE_LIMIT_HOLD_S
            # every password of the account waits: the limit counts per account
            self._held[(key[0], "*")] = (self.clock() + hold, error)
        else:
            hold = UNAVAILABLE_HOLD_S
        self._held[key] = (self.clock() + hold, error)

    def check_account(self, key: PoolKey) -> None:
        """Also honour a rate limit held for the whole account."""
        self.check((key[0], "*"))
        self.check(key)

    def succeeded(self, key: PoolKey) -> None:
        """A login worked: nothing held for these credentials, and their next login waits
        :data:`MIN_LOGIN_INTERVAL_S`."""
        self._held.pop(key, None)
        self._last_login[key] = self.clock()


@dataclass(eq=False, slots=True)
class _Entry:
    key: PoolKey
    session: WorxSession
    refs: int = 0
    linger: asyncio.Task[None] | None = field(default=None, repr=False)


class AccountLease:
    """A share of one pooled session; :meth:`release` once (idempotent)."""

    def __init__(self, pool: "AccountPool", entry: _Entry, *, reused: bool) -> None:
        self._pool = pool
        self._entry: _Entry | None = entry
        self.session = entry.session
        self.reused = reused
        """True when the session was already open (no login for this lease)."""

    async def release(self) -> None:
        """Give the share back; the last one stops the session (after the linger time)."""
        entry, self._entry = self._entry, None
        if entry is not None:
            await self._pool._release(entry)


class AccountPool:
    """Reference counted sessions, one per account and password, behind a :class:`LoginGuard`."""

    def __init__(
        self,
        opener: SessionOpener,
        *,
        linger_seconds: float = DEFAULT_LINGER_SECONDS,
        guard: LoginGuard | None = None,
    ) -> None:
        self._opener = opener
        self._linger_seconds = linger_seconds
        self.guard = guard if guard is not None else LoginGuard()
        self._entries: dict[PoolKey, _Entry] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._closed = False
        self.logins = 0
        """Logins sent so far (tests and metrics)."""

    @property
    def open_sessions(self) -> int:
        """Sessions currently held (in use or lingering)."""
        return len(self._entries)

    def users(self, login: WorxLogin) -> int:
        """Leases on the session of these credentials (0 when none is open)."""
        entry = self._entries.get(pool_key(login))
        return entry.refs if entry is not None else 0

    async def acquire(self, login: WorxLogin) -> AccountLease:
        """A lease on the session of ``login``, logging in when none is open. Concurrent
        acquires of one account wait for one login; a failed login leaves nothing behind and is
        held by the guard."""
        if self._closed:
            raise PoolClosedError("the Worx session pool is closed")
        key = pool_key(login)
        lock = self._locks.setdefault(key[0], asyncio.Lock())
        async with lock:
            entry = self._entries.get(key)
            if entry is not None and not entry.session.alive:
                self._entries.pop(key, None)
                entry = None
            reused = entry is not None
            if entry is None:
                await self._replace_other_passwords(key)
                self.guard.check_account(key)
                self.logins += 1
                try:
                    session = await self._opener(login)
                except Exception as exc:
                    error = (
                        exc
                        if isinstance(exc, CloudUnavailable | CloudLoginRefused)
                        else CloudUnavailable("the Worx cloud did not answer")
                    )
                    self.guard.failed(key, error)
                    if error is exc:
                        raise
                    raise error from exc
                self.guard.succeeded(key)
                if self._closed:
                    await session.stop()
                    raise PoolClosedError("the Worx session pool is closed")
                entry = _Entry(key, session)
                self._entries[key] = entry
            if entry.linger is not None:
                entry.linger.cancel()
                entry.linger = None
            entry.refs += 1
        return AccountLease(self, entry, reused=reused)

    async def _replace_other_passwords(self, key: PoolKey) -> None:
        """A new password of the account: stop the idle sessions of its old one (one session
        per account at the cloud; sessions still in use keep running until released)."""
        for other in [e for k, e in self._entries.items() if k[0] == key[0] and k != key]:
            if other.refs == 0:
                if other.linger is not None:
                    other.linger.cancel()
                    other.linger = None
                await self._stop(other)

    async def _release(self, entry: _Entry) -> None:
        entry.refs = max(0, entry.refs - 1)
        if entry.refs > 0:
            return
        if self._entries.get(entry.key) is not entry or self._linger_seconds <= 0:
            await self._stop(entry)
            return
        entry.linger = asyncio.get_running_loop().create_task(self._expire(entry))

    async def _expire(self, entry: _Entry) -> None:
        await asyncio.sleep(self._linger_seconds)
        if entry.refs > 0 or self._entries.get(entry.key) is not entry:
            return
        entry.linger = None
        await self._stop(entry)

    async def _stop(self, entry: _Entry) -> None:
        if self._entries.get(entry.key) is entry:
            self._entries.pop(entry.key, None)
        try:
            await entry.session.stop()
        except Exception:
            _log.exception("stopping a Worx session failed")

    async def close(self) -> None:
        """Stop every session (shutdown); later acquires raise :class:`PoolClosedError`."""
        self._closed = True
        for entry in list(self._entries.values()):
            if entry.linger is not None:
                entry.linger.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await entry.linger
                entry.linger = None
            await self._stop(entry)
        self._entries.clear()
        self._locks.clear()
