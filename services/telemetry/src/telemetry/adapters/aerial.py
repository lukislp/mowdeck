"""The tile sources (HTTP) and the tile cache (a bounded directory on disk).

Every upstream request identifies MowDeck (``User-Agent: MowDeck/<version> (+<repo>)``), sends no
``Referer`` and follows no redirect; a tile is kept as long as the source's ``Cache-Control`` or
``Expires`` allows (at least 7 days without either) and never fetched again before that. This is
what the OpenStreetMap tile usage policy asks of a server that uses its tiles.
"""

import asyncio
import contextlib
import os
import re
import time
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
from pathlib import Path

import httpx
from mowdeck_service_kit import get_logger

from telemetry.application.aerial import MAX_TILE_BYTES, FetchedTile
from telemetry.domain.errors import AerialUpstreamError

_log = get_logger("telemetry.aerial")

_PNG = b"\x89PNG\r\n\x1a\n"
_JPEG = b"\xff\xd8\xff"


PROJECT_URL = "https://github.com/lukislp/mowdeck"


def user_agent(version: str | None) -> str:
    """The identifying ``User-Agent`` of every upstream tile request."""
    return f"MowDeck/{version or 'dev'} (+{PROJECT_URL})"


_MAX_AGE = re.compile(r"(?:^|,)\s*max-age\s*=\s*\"?(\d+)\"?", re.IGNORECASE)
_NO_STORE = re.compile(r"(?:^|,)\s*(?:no-store|no-cache)\b", re.IGNORECASE)


def cache_lifetime_of(headers: httpx.Headers, now: datetime) -> timedelta | None:
    """How long an answer may be reused according to ``Cache-Control`` (``max-age`` wins,
    ``no-store``/``no-cache`` mean not at all) or ``Expires``; None when neither says."""
    control = headers.get("cache-control", "")
    if _NO_STORE.search(control):
        return timedelta(0)
    if match := _MAX_AGE.search(control):
        return timedelta(seconds=int(match.group(1)))
    expires = headers.get("expires")
    if expires:
        try:
            when = parsedate_to_datetime(expires)
        except (TypeError, ValueError):
            return timedelta(0)  # an invalid Expires means already expired (RFC 9111)
        if when.tzinfo is None:
            when = when.replace(tzinfo=UTC)
        return max(timedelta(0), when - now)
    return None


def sniff_media_type(body: bytes) -> str | None:
    """``image/png`` or ``image/jpeg`` by magic bytes; None for anything else (a WMS answers
    a service exception as XML with status 200)."""
    if body.startswith(_PNG):
        return "image/png"
    if body.startswith(_JPEG):
        return "image/jpeg"
    return None


class HttpTileSource:
    """Fetches tiles with one shared client; redirects are not followed."""

    def __init__(
        self,
        timeout_seconds: float = 10.0,
        client: httpx.AsyncClient | None = None,
        *,
        version: str | None = None,
    ):
        self._client = client or httpx.AsyncClient(timeout=timeout_seconds)
        self._headers = {"User-Agent": user_agent(version)}

    async def fetch(self, url: str) -> FetchedTile:
        """GET ``url``; the answer must be a PNG or JPEG of reasonable size."""
        body = bytearray()
        try:
            async with self._client.stream(
                "GET", url, headers=self._headers, follow_redirects=False
            ) as response:
                if response.status_code != 200:
                    msg = f"the aerial photo source answered {response.status_code}"
                    raise AerialUpstreamError(msg)
                lifetime = cache_lifetime_of(response.headers, datetime.now(UTC))
                async for chunk in response.aiter_bytes():
                    body.extend(chunk)
                    if len(body) > MAX_TILE_BYTES:
                        msg = "the aerial photo source sent an oversized image"
                        raise AerialUpstreamError(msg)
        except httpx.HTTPError as exc:
            _log.warning("aerial source unreachable", error=type(exc).__name__)
            msg = "the aerial photo source is unreachable"
            raise AerialUpstreamError(msg) from exc
        media_type = sniff_media_type(bytes(body))
        if media_type is None:
            msg = "the aerial photo source did not send an image"
            raise AerialUpstreamError(msg)
        return FetchedTile(bytes(body), media_type, lifetime)

    async def close(self) -> None:
        """Close the client."""
        await self._client.aclose()


class DiskTileCache:
    """Tiles as files named by the hash of their upstream URL; a file's modification time is
    the moment it expires, so an expired file is ignored (and replaced on the next write). When
    the directory grows beyond ``max_bytes`` the files expiring first are deleted down to 90
    percent. A directory that cannot be written is logged once and the cache then just misses."""

    def __init__(self, directory: Path, max_bytes: int) -> None:
        self._dir = directory
        self._max = max_bytes
        self._writes = 0
        self._warned = False

    def _path(self, key: str) -> Path:
        return self._dir / key[:2] / key

    def _read(self, key: str) -> FetchedTile | None:
        path = self._path(key)
        try:
            if path.stat().st_mtime <= time.time():
                return None
            body = path.read_bytes()
        except OSError:
            return None
        media_type = sniff_media_type(body)
        return FetchedTile(body, media_type) if media_type else None

    def _write(self, key: str, tile: FetchedTile, lifetime: timedelta) -> None:
        path = self._path(key)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(f".{os.getpid()}.tmp")
            tmp.write_bytes(tile.body)
            expires = time.time() + lifetime.total_seconds()
            os.utime(tmp, (expires, expires))
            tmp.replace(path)
        except OSError as exc:
            if not self._warned:
                self._warned = True
                _log.warning("aerial cache not writable", directory=str(self._dir), error=str(exc))
            return
        self._writes += 1
        if self._writes % 50 == 0:
            self.trim()

    def trim(self) -> None:
        """Delete the files expiring first until the directory is at 90 percent of its limit."""
        files: list[tuple[float, int, Path]] = []
        total = 0
        with contextlib.suppress(OSError):
            for path in self._dir.rglob("*"):
                if path.is_file():
                    st = path.stat()
                    files.append((st.st_mtime, st.st_size, path))
                    total += st.st_size
        if total <= self._max:
            return
        files.sort()
        for _, size, path in files:
            if total <= self._max * 0.9:
                break
            with contextlib.suppress(OSError):
                path.unlink()
                total -= size

    async def get(self, key: str) -> FetchedTile | None:
        """The cached tile when present and not expired."""
        return await asyncio.to_thread(self._read, key)

    async def put(self, key: str, tile: FetchedTile, lifetime: timedelta) -> None:
        """Store a tile for ``lifetime``."""
        await asyncio.to_thread(self._write, key, tile, lifetime)

    def size_bytes(self) -> int:
        """Total size of the cached files."""
        return sum(p.stat().st_size for p in self._dir.rglob("*") if p.is_file())
