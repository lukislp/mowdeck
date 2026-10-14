"""Public keys for verification: a cached JWKS fetched over HTTP, or a static set.

:class:`JwksCache` mirrors PyJWT's ``PyJWKClient`` (cached set with a lifespan, one forced
refresh on an unknown ``kid``) but is async and rate limited:

* the set is reused for ``lifespan`` seconds;
* an unknown ``kid`` triggers a refresh, so a freshly rotated key verifies at once;
* two fetches are at least ``refresh_cooldown`` seconds apart, so a flood of tokens with random
  ``kid`` values cannot turn the service into a JWKS fetching proxy;
* when a refresh fails the last good set is served for at most ``stale_max`` seconds after the
  last success (logged), then dropped (every token is rejected until Identity answers again).
"""

import asyncio
import logging
import time
from collections.abc import Callable, Iterable, Mapping
from typing import Any, Protocol, runtime_checkable

import httpx
from jwt import PyJWK
from jwt.exceptions import PyJWTError

_log = logging.getLogger("mowdeck_auth.jwks")


@runtime_checkable
class KeySource(Protocol):
    """Finds the public key for a ``kid``."""

    async def key_for(self, kid: str) -> PyJWK | None:
        """The key, or None if no key with that id is known."""
        ...


def parse_jwks(document: Mapping[str, Any]) -> dict[str, PyJWK]:
    """Parse a JWKS document into ``{kid: PyJWK}``; unusable entries are skipped."""
    keys: dict[str, PyJWK] = {}
    raw_keys = document.get("keys")
    if not isinstance(raw_keys, list):
        msg = "JWKS document has no 'keys' list"
        raise ValueError(msg)
    for entry in raw_keys:
        if not isinstance(entry, dict) or not isinstance(entry.get("kid"), str):
            continue
        if entry.get("use", "sig") != "sig":
            continue
        try:
            keys[entry["kid"]] = PyJWK.from_dict(entry)
        except (PyJWTError, ValueError, TypeError):
            _log.warning("skipping unusable JWK", extra={"kid": entry.get("kid")})
    return keys


class StaticKeySource:
    """A fixed key set (tests, or a service that receives keys another way)."""

    def __init__(self, keys: Iterable[Mapping[str, Any]] | Mapping[str, Any] = ()) -> None:
        self._keys: dict[str, PyJWK] = {}
        self.replace(keys)

    def replace(self, keys: Iterable[Mapping[str, Any]] | Mapping[str, Any]) -> None:
        """Replace the set with JWK dicts or a JWKS document (``{"keys": [...]}``)."""
        document = keys if isinstance(keys, Mapping) else {"keys": list(keys)}
        self._keys = parse_jwks(document)

    async def key_for(self, kid: str) -> PyJWK | None:
        """The key with that id, if present."""
        return self._keys.get(kid)


class JwksCache:
    """Fetches and caches the JWKS of Identity (see the module docstring for the rules)."""

    def __init__(
        self,
        url: str,
        *,
        lifespan: float = 300.0,
        refresh_cooldown: float = 30.0,
        stale_max: float = 3600.0,
        timeout: float = 3.0,
        transport: httpx.AsyncBaseTransport | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.url = url
        self.lifespan = lifespan
        self.refresh_cooldown = refresh_cooldown
        self.stale_max = stale_max
        self.timeout = timeout
        self._transport = transport
        self._clock = clock
        self._keys: dict[str, PyJWK] = {}
        self._fetched_at: float | None = None
        self._attempted_at: float | None = None
        self._lock = asyncio.Lock()
        self._last_ok = False
        self.fetches = 0
        """Number of HTTP fetches attempted (for tests and metrics)."""

    def _may_fetch(self, now: float) -> bool:
        return self._attempted_at is None or now - self._attempted_at >= self.refresh_cooldown

    def _expired(self, now: float) -> bool:
        return self._fetched_at is None or now - self._fetched_at >= self.lifespan

    async def key_for(self, kid: str) -> PyJWK | None:
        """The key for ``kid``; refreshes when the set is old or the kid is unknown."""
        now = self._clock()
        if self._expired(now) and self._may_fetch(now):
            await self.refresh()
        key = self._keys.get(kid)
        if key is None and self._may_fetch(self._clock()):
            await self.refresh()
            key = self._keys.get(kid)
        return key

    async def refresh(self) -> bool:
        """Fetch the set now. Concurrent callers share one fetch. True on success."""
        seen = self.fetches
        async with self._lock:
            if self.fetches != seen:  # another caller fetched while this one waited
                return self._last_ok
            now = self._clock()
            self._attempted_at = now
            self.fetches += 1
            try:
                async with httpx.AsyncClient(
                    transport=self._transport, timeout=self.timeout
                ) as client:
                    response = await client.get(self.url, headers={"Accept": "application/json"})
                    response.raise_for_status()
                    keys = parse_jwks(response.json())
            except (httpx.HTTPError, ValueError) as exc:
                _log.warning(
                    "fetching the JWKS failed", extra={"url": self.url, "error": type(exc).__name__}
                )
                if self._fetched_at is not None and now - self._fetched_at > self.stale_max:
                    _log.warning(
                        "dropping a JWKS that is too old to trust", extra={"url": self.url}
                    )
                    self._keys = {}
                    self._fetched_at = None
                self._last_ok = False
                return False
            self._keys = keys
            self._fetched_at = now
            self._last_ok = True
            return True
