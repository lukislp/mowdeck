"""One shared Mammotion cloud session per set of credentials (ADR 0034).

PyMammotion's ``MammotionClient`` holds every device of the account it logged in with, and a
second login with the same account supersedes the first one at the Mammotion cloud. So the
cloud connections of all mowers of one linked account, and the device discovery for that
account, share one client: :class:`CloudSessionPool` logs in once, hands out reference counted
:class:`SessionLease` objects and stops the client when the last lease is released (after
``linger_seconds``, so a discovery followed by the registration of the listed mowers needs one
login, not two).

The pool key is the lower-case e-mail and a random slot id per password: the pool compares a
password with the ones it already holds (``hmac.compare_digest``, constant time) and gives each
distinct one its own slot, so the same credentials share one session and a changed password
opens a new one. No hash or other derivative of a password is computed or kept, and neither the
address nor the password is written to the log (the log shows a short tag of the address).
When a login with a new password succeeds, unused sessions of the old password for the same
address are stopped at once, and an old session still in use is stopped as soon as its last
lease is released (no linger). A session whose login died
(``reauth_required``) is replaced on the next :meth:`CloudSessionPool.acquire`; the leases that
still point at it see ``is_open`` False and are released by their connections' reconnects.

Credential cache: with ``cache_dir`` each address gets its own PyMammotion credential cache
``<cache_dir>/<sha256 tag of the address>.json`` (live tokens: a private, writable volume); the
per-mower ``session_cache_path`` option is not used for shared sessions.

This module does not import PyMammotion: the login function is injected (``cloud.py`` passes the
real one, tests a fake), so the pool logic is tested without the network.
"""

import asyncio
import contextlib
import hashlib
import hmac
import logging
import secrets
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

_log = logging.getLogger("mowdeck_control_mammotion.session")

DEFAULT_LINGER_SECONDS = 120.0
"""How long an unused session stays open (discovery, then registering the listed mowers)."""

SessionKey = tuple[str, str]
"""``(lower-case e-mail, random slot id of the password)``."""


class PooledClient(Protocol):
    """What the pool needs from a client (``MammotionClient`` satisfies it)."""

    @property
    def reauth_required(self) -> str | None:
        """Reason the login is terminally dead, None while healthy."""
        ...

    async def stop(self) -> None:
        """Stop every transport of the client."""
        ...


LoginFunction = Callable[[str, str, Path | None], Awaitable[PooledClient]]
"""``login(account, password, cache_path)``: a logged-in client; raises on failure."""


def same_password(a: str, b: str) -> bool:
    """Constant-time comparison of two passwords (nothing is derived from them)."""
    return hmac.compare_digest(a.encode("utf-8"), b.encode("utf-8"))


def address_tag(account: str) -> str:
    """A short, stable tag of an address for logs and cache file names (not reversible)."""
    digest = hashlib.sha256(("mowdeck-session:" + account.strip().lower()).encode("utf-8"))
    return digest.hexdigest()[:24]


class PoolClosedError(RuntimeError):
    """The pool was closed (the gateway shuts down)."""


@dataclass(eq=False, slots=True)
class _Entry:
    key: SessionKey
    tag: str
    client: PooledClient
    refs: int = 0
    stopped: bool = False
    linger: asyncio.Task[None] | None = field(default=None, repr=False)

    @property
    def alive(self) -> bool:
        return not self.stopped and self.client.reauth_required is None


class SessionLease:
    """A share of one pooled session; :meth:`release` exactly once (idempotent)."""

    def __init__(self, pool: "CloudSessionPool", entry: _Entry, *, reused: bool) -> None:
        self._pool = pool
        self._entry: _Entry | None = entry
        self.client = entry.client
        self.reused = reused
        """True when the session was already open (no login for this lease)."""

    @property
    def released(self) -> bool:
        """True after :meth:`release`."""
        return self._entry is None

    async def release(self) -> None:
        """Give the share back; the last one stops the client (after the linger time)."""
        entry, self._entry = self._entry, None
        if entry is not None:
            await self._pool._release(entry)


class CloudSessionPool:
    """Reference counted cloud sessions, one per set of credentials."""

    def __init__(
        self,
        login: LoginFunction,
        *,
        linger_seconds: float = DEFAULT_LINGER_SECONDS,
        cache_dir: Path | None = None,
    ) -> None:
        self._login = login
        self._linger_seconds = linger_seconds
        self._cache_dir = cache_dir
        self._entries: dict[SessionKey, _Entry] = {}
        self._locks: dict[SessionKey, asyncio.Lock] = {}
        self._slots: dict[str, list[tuple[str, str]]] = {}
        """Per lower-case address: (password, slot id) of the passwords seen (never logged)."""
        self._closed = False
        self.logins = 0
        """Number of logins so far (metrics and tests)."""

    def session_key(self, account: str, password: str, *, create: bool = True) -> SessionKey | None:
        """The pool key of a set of credentials: the lower-case address and the slot of the
        password (a new slot for a password not seen yet; None for one with ``create`` False)."""
        address = account.strip().lower()
        slots = self._slots.setdefault(address, [])
        for known, slot in slots:
            if same_password(known, password):
                return address, slot
        if not create:
            return None
        slot = secrets.token_hex(8)
        slots.append((password, slot))
        return address, slot

    def _entry_of(self, account: str, password: str) -> _Entry | None:
        key = self.session_key(account, password, create=False)
        return self._entries.get(key) if key is not None else None

    def _prune(self, address: str) -> None:
        """Forget the passwords of an address that no session and no login waits for."""
        used = {k[1] for k in self._entries if k[0] == address}
        used |= {k[1] for k, lock in self._locks.items() if k[0] == address and lock.locked()}
        kept = [(p, s) for p, s in self._slots.get(address, []) if s in used]
        if kept:
            self._slots[address] = kept
        else:
            self._slots.pop(address, None)

    def is_open(self, account: str, password: str) -> bool:
        """True while a live session for these credentials exists."""
        entry = self._entry_of(account, password)
        return entry is not None and entry.alive

    def users(self, account: str, password: str) -> int:
        """Number of leases on the session of these credentials (0 when none is open)."""
        entry = self._entry_of(account, password)
        return entry.refs if entry is not None else 0

    @property
    def open_sessions(self) -> int:
        """Number of sessions currently held (live or lingering)."""
        return len(self._entries)

    def cache_path(self, account: str) -> Path | None:
        """The credential cache file of ``account`` (None without a cache directory)."""
        if self._cache_dir is None:
            return None
        return self._cache_dir / f"{address_tag(account)}.json"

    async def acquire(self, account: str, password: str) -> SessionLease:
        """A lease on the session of these credentials, logging in when none is open.

        Concurrent acquires for the same credentials wait for one login. A failed login raises
        and leaves no session behind; after :meth:`close` it raises :class:`PoolClosedError`.
        """
        if self._closed:
            raise PoolClosedError("the cloud session pool is closed")
        key = self.session_key(account, password)
        assert key is not None  # noqa: S101 - created on demand
        lock = self._locks.setdefault(key, asyncio.Lock())
        try:
            return await self._acquire_locked(key, lock, account, password)
        finally:
            self._prune(key[0])

    async def _acquire_locked(
        self, key: SessionKey, lock: asyncio.Lock, account: str, password: str
    ) -> SessionLease:
        async with lock:
            entry = self._entries.get(key)
            if entry is not None and not entry.alive:
                _log.info("replacing a dead Mammotion cloud session of %s", entry.tag[:8])
                self._entries.pop(key, None)
                if entry.refs == 0:
                    await self._stop(entry)
                entry = None
            reused = entry is not None
            if entry is None:
                client = await self._login(account, password, self.cache_path(account))
                if self._closed:
                    await client.stop()
                    raise PoolClosedError("the cloud session pool is closed")
                self.logins += 1
                entry = _Entry(key, address_tag(account), client)
                self._entries[key] = entry
                await self._retire_older(entry)
            if entry.linger is not None:
                entry.linger.cancel()
                entry.linger = None
            entry.refs += 1
        return SessionLease(self, entry, reused=reused)

    async def _retire_older(self, fresh: _Entry) -> None:
        """A login with new credentials for the same address succeeded: stop the unused
        sessions of the old ones (sessions in use stop on their last release)."""
        for other in list(self._entries.values()):
            if other is fresh or other.key[0] != fresh.key[0] or other.refs > 0:
                continue
            self._forget(other)
            await self._stop(other)

    def _superseded(self, entry: _Entry) -> bool:
        return any(
            other is not entry and other.key[0] == entry.key[0] and other.alive
            for other in self._entries.values()
        )

    def _forget(self, entry: _Entry) -> None:
        if self._entries.get(entry.key) is entry:
            self._entries.pop(entry.key, None)
        lock = self._locks.get(entry.key)
        if entry.key not in self._entries and lock is not None and not lock.locked():
            self._locks.pop(entry.key, None)
        self._prune(entry.key[0])

    async def _release(self, entry: _Entry) -> None:
        entry.refs = max(0, entry.refs - 1)
        if entry.refs > 0:
            return
        stale = self._entries.get(entry.key) is not entry or not entry.alive
        if stale or self._linger_seconds <= 0 or self._superseded(entry):
            self._forget(entry)
            await self._stop(entry)
            return
        entry.linger = asyncio.get_running_loop().create_task(self._expire(entry))

    async def _expire(self, entry: _Entry) -> None:
        await asyncio.sleep(self._linger_seconds)
        lock = self._locks.setdefault(entry.key, asyncio.Lock())
        async with lock:
            if entry.refs > 0 or self._entries.get(entry.key) is not entry:
                return
            entry.linger = None
            self._entries.pop(entry.key, None)
        self._forget(entry)
        await self._stop(entry)

    async def _stop(self, entry: _Entry) -> None:
        if entry.stopped:
            return
        entry.stopped = True
        if entry.linger is not None and entry.linger is not asyncio.current_task():
            entry.linger.cancel()
        try:
            await entry.client.stop()
        except Exception:
            _log.exception("stopping a pooled Mammotion client failed")

    async def close(self) -> None:
        """Stop every session (shutdown). Leases still held become unusable; later acquires
        raise :class:`PoolClosedError`."""
        self._closed = True
        entries = list(self._entries.values())
        self._entries.clear()
        self._locks.clear()
        self._slots.clear()
        for entry in entries:
            if entry.linger is not None:
                entry.linger.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await entry.linger
                entry.linger = None
            await self._stop(entry)
