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
  refused again without asking the cloud (a corrected password is tried at once);
* a **rate limited** login (HTTP 429) for :data:`RATE_LIMIT_HOLD_S`, and any other failed login
  for :data:`UNAVAILABLE_HOLD_S`;
* every successful login, so the next one with the same credentials waits at least
  :data:`MIN_LOGIN_INTERVAL_S` (a session that died a moment ago is not replaced at once; a
  corrected or changed password is tried at once).

Sessions and holds are keyed by the account (brand and lower-case e-mail, :func:`account_of`).
Which password they belong to is decided by comparing the password itself
(``hmac.compare_digest``, constant time) with the one the session already holds for its login:
no hash, digest or other derivative of a password is computed or kept, and none is logged.
"""

import asyncio
import contextlib
import hmac
import logging
import math
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
"""Shortest time between two logins with the same credentials."""


def account_of(login: WorxLogin) -> str:
    """The account a login belongs to: brand and lower-case e-mail (no secret)."""
    return f"{login.brand}:{login.email.strip().casefold()}"


def same_password(a: str, b: str) -> bool:
    """Constant-time comparison of two passwords (nothing is derived from them)."""
    return hmac.compare_digest(a.encode("utf-8"), b.encode("utf-8"))


class PoolClosedError(CloudUnavailable):
    """The pool was closed (the gateway shuts down)."""


def _minutes(seconds: float) -> int:
    return max(1, math.ceil(seconds / 60))


@dataclass(slots=True)
class _Hold:
    until: float
    error: Exception
    password: str | None = field(default=None, repr=False)
    """The refused password; None holds every password of the account."""


@dataclass(slots=True)
class LoginGuard:
    """Refuses logins the cloud should not see now (see the module docstring)."""

    clock: Callable[[], float] = time.monotonic
    _held: dict[str, list[_Hold]] = field(default_factory=dict)
    _last_login: dict[str, tuple[float, str]] = field(default_factory=dict, repr=False)

    def check(self, login: WorxLogin) -> None:
        """Raise the held failure, or :class:`CloudUnavailable` while these credentials logged
        in a moment ago; return when a login may go out."""
        now, account = self.clock(), account_of(login)
        holds = [h for h in self._held.get(account, []) if h.until > now]
        self._held[account] = holds
        for hold in holds:
            if hold.password is not None and not same_password(hold.password, login.password):
                continue
            wait = _minutes(hold.until - now)
            if isinstance(hold.error, CloudLoginRefused):
                msg = f"{hold.error} (not tried again for {wait} min)"
                raise CloudLoginRefused(msg)
            msg = f"{hold.error} (MowDeck waits {wait} min before the next login)"
            raise CloudUnavailable(msg)
        last = self._last_login.get(account)
        recent = last is not None and now - last[0] < MIN_LOGIN_INTERVAL_S
        if last is not None and recent and same_password(last[1], login.password):
            wait_s = math.ceil(MIN_LOGIN_INTERVAL_S - (now - last[0]))
            msg = f"the account logged in a moment ago; MowDeck waits {wait_s} s to log in again"
            raise CloudUnavailable(msg)

    def failed(self, login: WorxLogin, error: Exception) -> None:
        """The login failed with ``error``: hold the credentials (or the account) for a while."""
        account, now = account_of(login), self.clock()
        holds = self._held.setdefault(account, [])
        if isinstance(error, CloudLoginRefused):
            holds.append(_Hold(now + REFUSED_HOLD_S, error, login.password))
        elif isinstance(error, LoginRateLimited):
            holds.append(_Hold(now + RATE_LIMIT_HOLD_S, error))  # the limit counts per account
        else:
            holds.append(_Hold(now + UNAVAILABLE_HOLD_S, error, login.password))

    def succeeded(self, login: WorxLogin) -> None:
        """A login worked: nothing held for these credentials, and their next login waits
        :data:`MIN_LOGIN_INTERVAL_S`."""
        account = account_of(login)
        self._held[account] = [
            h
            for h in self._held.get(account, [])
            if h.password is None or not same_password(h.password, login.password)
        ]
        self._last_login[account] = (self.clock(), login.password)


@dataclass(eq=False, slots=True)
class _Entry:
    account: str
    login: WorxLogin = field(repr=False)
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
        self._entries: dict[str, list[_Entry]] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._closed = False
        self.logins = 0
        """Logins sent so far (tests and metrics)."""

    @property
    def open_sessions(self) -> int:
        """Sessions currently held (in use or lingering)."""
        return sum(len(entries) for entries in self._entries.values())

    def _find(self, login: WorxLogin) -> _Entry | None:
        for entry in self._entries.get(account_of(login), []):
            if same_password(entry.login.password, login.password):
                return entry
        return None

    def users(self, login: WorxLogin) -> int:
        """Leases on the session of these credentials (0 when none is open)."""
        entry = self._find(login)
        return entry.refs if entry is not None else 0

    def _drop(self, entry: _Entry) -> None:
        entries = self._entries.get(entry.account, [])
        if entry in entries:
            entries.remove(entry)
        if not entries:
            self._entries.pop(entry.account, None)

    async def acquire(self, login: WorxLogin) -> AccountLease:
        """A lease on the session of ``login``, logging in when none is open. Concurrent
        acquires of one account wait for one login; a failed login leaves nothing behind and is
        held by the guard."""
        if self._closed:
            raise PoolClosedError("the Worx session pool is closed")
        account = account_of(login)
        lock = self._locks.setdefault(account, asyncio.Lock())
        async with lock:
            entry = self._find(login)
            if entry is not None and not entry.session.alive:
                self._drop(entry)
                entry = None
            reused = entry is not None
            if entry is None:
                await self._replace_other_passwords(login)
                self.guard.check(login)
                self.logins += 1
                try:
                    session = await self._opener(login)
                except Exception as exc:
                    error = (
                        exc
                        if isinstance(exc, CloudUnavailable | CloudLoginRefused)
                        else CloudUnavailable("the Worx cloud did not answer")
                    )
                    self.guard.failed(login, error)
                    if error is exc:
                        raise
                    raise error from exc
                self.guard.succeeded(login)
                if self._closed:
                    await session.stop()
                    raise PoolClosedError("the Worx session pool is closed")
                entry = _Entry(account, login, session)
                self._entries.setdefault(account, []).append(entry)
            if entry.linger is not None:
                entry.linger.cancel()
                entry.linger = None
            entry.refs += 1
        return AccountLease(self, entry, reused=reused)

    async def _replace_other_passwords(self, login: WorxLogin) -> None:
        """A new password of the account: stop the idle sessions of its old one (one session
        per account at the cloud; sessions still in use keep running until released)."""
        for other in list(self._entries.get(account_of(login), [])):
            if other.refs == 0 and not same_password(other.login.password, login.password):
                if other.linger is not None:
                    other.linger.cancel()
                    other.linger = None
                await self._stop(other)

    async def _release(self, entry: _Entry) -> None:
        entry.refs = max(0, entry.refs - 1)
        if entry.refs > 0:
            return
        if entry not in self._entries.get(entry.account, []) or self._linger_seconds <= 0:
            await self._stop(entry)
            return
        entry.linger = asyncio.get_running_loop().create_task(self._expire(entry))

    async def _expire(self, entry: _Entry) -> None:
        await asyncio.sleep(self._linger_seconds)
        if entry.refs > 0 or entry not in self._entries.get(entry.account, []):
            return
        entry.linger = None
        await self._stop(entry)

    async def _stop(self, entry: _Entry) -> None:
        self._drop(entry)
        try:
            await entry.session.stop()
        except Exception:
            _log.exception("stopping a Worx session failed")

    async def close(self) -> None:
        """Stop every session (shutdown); later acquires raise :class:`PoolClosedError`."""
        self._closed = True
        for entry in [e for entries in self._entries.values() for e in entries]:
            if entry.linger is not None:
                entry.linger.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await entry.linger
                entry.linger = None
            await self._stop(entry)
        self._entries.clear()
        self._locks.clear()
