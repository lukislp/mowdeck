"""The aerial photo under the mower map: tiles for the area around the account's map only.

Telemetry is the one place that knows the map (the geographic reference and every outline) and
who may see it. A tile is served when its EPSG:3857 box overlaps the map's bounding box plus a
margin and its zoom is in the configured range; everything else is refused, so this is not an
open proxy to the photo source. A tile that is not cached is fetched once from the configured
WMS (or XYZ) source and kept on disk; fetches of new tiles are limited per account. The tile
bytes are the single product of :meth:`Aerial.tile`, so a server side composite of map and photo
(widget, notification image) can call it with the same arguments as the browser.
"""

import asyncio
import hashlib
import time
from collections import defaultdict, deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import timedelta
from typing import Protocol
from uuid import UUID

from mowdeck_contracts.v1 import MowerMap

from telemetry.application.queries import Queries
from telemetry.domain.aerial import (
    Bounds,
    map_bounds,
    points_bounds,
    tile_bounds,
    tile_in_range,
    wms_get_map_url,
    xyz_url,
)
from telemetry.domain.errors import (
    AerialDisabledError,
    AerialRateLimitedError,
    TileOutsideMapError,
)

MAX_TILE_BYTES = 2_000_000
"""Largest image accepted from the photo source."""

ZonePoints = Callable[[UUID, UUID], Awaitable[list[tuple[float, float]]]]
"""The ``(lat, lon)`` corners of a mower's virtual zones, by account and mower."""


@dataclass(frozen=True, slots=True)
class AerialConfig:
    """Where the photos come from and how they are cached (from the settings)."""

    wms_url: str | None = None
    wms_layer: str = ""
    wms_version: str = "1.3.0"
    image_format: str = "image/jpeg"
    xyz_url: str | None = None
    attribution: str = ""
    min_zoom: int = 14
    max_zoom: int = 19
    tile_size: int = 256
    margin_m: float = 60.0
    cache_ttl: timedelta = timedelta(days=30)
    misses_per_minute: int = 240

    @property
    def enabled(self) -> bool:
        """Whether a source is configured."""
        return bool(self.xyz_url) or (bool(self.wms_url) and bool(self.wms_layer))


@dataclass(frozen=True, slots=True)
class FetchedTile:
    """An image from a tile source."""

    body: bytes
    media_type: str
    lifetime: timedelta | None = None
    """How long the source allows the tile to be reused (``Cache-Control``/``Expires``); None
    when it said nothing."""


class TileSource(Protocol):
    """Fetches an image by URL; raises ``AerialUpstreamError`` when there is none."""

    async def fetch(self, url: str) -> FetchedTile: ...


class TileCache(Protocol):
    """Keeps fetched tiles on disk until they expire."""

    async def get(self, key: str) -> FetchedTile | None: ...

    async def put(self, key: str, tile: FetchedTile, lifetime: timedelta) -> None: ...


MIN_DEFAULT_LIFETIME = timedelta(days=7)
"""A tile without cache headers is kept at least this long (OSM tile usage policy)."""


def cache_lifetime(tile: FetchedTile, default: timedelta) -> timedelta:
    """How long to keep ``tile``: what its source said, else ``default`` but at least 7 days."""
    if tile.lifetime is not None:
        return tile.lifetime
    return max(default, MIN_DEFAULT_LIFETIME)


async def fetch_cached(
    url: str,
    source: TileSource,
    cache: TileCache | None,
    default_lifetime: timedelta,
    *,
    before_fetch: Callable[[], None],
    gate: asyncio.Semaphore,
) -> FetchedTile:
    """The tile at ``url`` from the cache while it is fresh, else fetched once from the source
    (``before_fetch`` may refuse by raising, e.g. a rate limit) and cached for its lifetime. No
    tile is ever fetched ahead of a request."""
    key = hashlib.sha256(url.encode()).hexdigest()
    if cache is not None:
        cached = await cache.get(key)
        if cached is not None:
            return cached
    before_fetch()
    async with gate:
        tile = await source.fetch(url)
    lifetime = cache_lifetime(tile, default_lifetime)
    if cache is not None and lifetime > timedelta(0):
        await cache.put(key, tile, lifetime)
    return tile


@dataclass(frozen=True, slots=True)
class AerialInfo:
    """What the client needs to lay the photo under the map."""

    enabled: bool
    attribution: str = ""
    min_zoom: int = 0
    max_zoom: int = 0
    bounds: Bounds | None = None


class MissLimiter:
    """At most ``limit`` new tiles per account and minute (sliding window)."""

    def __init__(self, limit: int, now: Callable[[], float] = time.monotonic) -> None:
        self._limit = limit
        self._now = now
        self._hits: dict[UUID, deque[float]] = defaultdict(deque)

    def take(self, account_id: UUID) -> int | None:
        """Count one miss; returns None when allowed, else the seconds to wait."""
        now = self._now()
        hits = self._hits[account_id]
        while hits and now - hits[0] >= 60.0:
            hits.popleft()
        if len(hits) >= self._limit:
            return max(1, int(60.0 - (now - hits[0])) + 1)
        hits.append(now)
        return None

    def forget(self, account_id: UUID) -> None:
        """Drop the window of an account (demo sandbox purge)."""
        self._hits.pop(account_id, None)


class Aerial:
    """Serves aerial tiles for the area around a mower's map."""

    def __init__(
        self,
        config: AerialConfig,
        queries: Queries,
        source: TileSource | None,
        cache: TileCache | None,
        *,
        now: Callable[[], float] = time.monotonic,
        concurrency: int = 4,
        zone_points: ZonePoints | None = None,
    ) -> None:
        self._config = config
        self._queries = queries
        self._source = source
        self._cache = cache
        self._limiter = MissLimiter(config.misses_per_minute, now)
        self._gate = asyncio.Semaphore(concurrency)
        self._zone_points = zone_points

    def forget_account(self, account_id: UUID) -> None:
        """Drop what is kept in memory per account (demo sandbox purge)."""
        self._limiter.forget(account_id)

    @property
    def enabled(self) -> bool:
        """Whether a source is configured."""
        return self._config.enabled and self._source is not None

    async def _map(self, account_id: UUID, mower_id: UUID) -> MowerMap:
        row = await self._queries.snapshot(account_id, mower_id, "map")
        return MowerMap.model_validate(row.body)

    async def _bounds(self, account_id: UUID, mower_id: UUID) -> Bounds:
        """The map's box plus margin, grown by the corners of the virtual zones (ADR 0070), so
        a zone drawn beyond the vendor's map still lies on the photo."""
        bounds = map_bounds(await self._map(account_id, mower_id), self._config.margin_m)
        if self._zone_points is not None:
            extra = points_bounds(
                await self._zone_points(account_id, mower_id), self._config.margin_m
            )
            if extra is not None:
                bounds = bounds.union(extra)
        return bounds

    async def info(self, account_id: UUID, mower_id: UUID) -> AerialInfo:
        """The source's attribution and zoom range with the box of the map plus margin."""
        bounds = await self._bounds(account_id, mower_id)
        if not self.enabled:
            return AerialInfo(enabled=False)
        return AerialInfo(
            enabled=True,
            attribution=self._config.attribution,
            min_zoom=self._config.min_zoom,
            max_zoom=self._config.max_zoom,
            bounds=bounds,
        )

    def upstream_url(self, z: int, x: int, y: int) -> str:
        """The URL of the photo source for tile ``z/x/y``."""
        cfg = self._config
        if cfg.xyz_url:
            return xyz_url(cfg.xyz_url, z, x, y)
        assert cfg.wms_url is not None  # noqa: S101 - enabled implies one of the two
        return wms_get_map_url(
            cfg.wms_url,
            layer=cfg.wms_layer,
            image_format=cfg.image_format,
            version=cfg.wms_version,
            z=z,
            x=x,
            y=y,
            size=cfg.tile_size,
        )

    async def tile(self, account_id: UUID, mower_id: UUID, z: int, x: int, y: int) -> FetchedTile:
        """The tile ``z/x/y`` of the photo, if it lies around the mower's map."""
        bounds = await self._bounds(account_id, mower_id)
        if not self.enabled or self._source is None:
            msg = "no aerial photo source is configured"
            raise AerialDisabledError(msg)
        cfg = self._config
        if not (tile_in_range(z, x, y) and cfg.min_zoom <= z <= cfg.max_zoom):
            msg = "the tile is outside the zoom range of the aerial photo"
            raise TileOutsideMapError(msg)
        if not bounds.intersects(tile_bounds(z, x, y)):
            msg = "the tile is not around the mower's map"
            raise TileOutsideMapError(msg)

        def take() -> None:
            wait = self._limiter.take(account_id)
            if wait is not None:
                msg = "too many new aerial tiles requested; try again shortly"
                raise AerialRateLimitedError(msg, wait)

        return await fetch_cached(
            self.upstream_url(z, x, y),
            self._source,
            self._cache,
            cfg.cache_ttl,
            before_fetch=take,
            gate=self._gate,
        )
