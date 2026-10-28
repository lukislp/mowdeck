"""The base map under a theft tracking track (ADR 0058): a street map and the aerial photo, only
around the track.

A stolen mower leaves the garden, so the aerial photo of the mower map (served only around the
garden, ADR 0054) cannot show where it is. This serves tiles of up to two layers, ``map`` (an
XYZ street map source, ``MOWDECK_TELEMETRY_BASEMAP_*``) and ``aerial`` (the configured aerial
photo source), for the box around the session's positions plus a margin. The browser never
contacts the source (the content security policy stays as it is, and the share page reveals
nothing beyond the track it already shows); the cache and the source are the aerial photo's.
Anything outside the box or the zoom range is refused, so this is no open proxy either.
"""

import asyncio
import hashlib
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import timedelta
from typing import Literal
from uuid import UUID

from telemetry.application.aerial import FetchedTile, MissLimiter, TileCache, TileSource
from telemetry.domain.aerial import Bounds, tile_bounds, tile_in_range, xyz_url
from telemetry.domain.errors import AerialRateLimitedError, TileOutsideMapError

LayerId = Literal["map", "aerial"]


@dataclass(frozen=True, slots=True)
class BaseLayer:
    """One base map layer: its credit line, zoom range and the URL of tile ``z/x/y``."""

    id: LayerId
    attribution: str
    min_zoom: int
    max_zoom: int
    url: Callable[[int, int, int], str]


def street_layer(template: str, attribution: str, min_zoom: int, max_zoom: int) -> BaseLayer:
    """The street map from an XYZ URL template."""
    return BaseLayer(
        id="map",
        attribution=attribution,
        min_zoom=min_zoom,
        max_zoom=max_zoom,
        url=lambda z, x, y: xyz_url(template, z, x, y),
    )


@dataclass(frozen=True, slots=True)
class TrackingMapConfig:
    """How far around the track tiles are served and how the cache behaves."""

    margin_m: float = 2000.0
    cache_ttl: timedelta = timedelta(days=30)
    misses_per_minute: int = 240


class TrackingMap:
    """Serves base map tiles around a track."""

    def __init__(
        self,
        layers: list[BaseLayer],
        source: TileSource | None,
        cache: TileCache | None,
        config: TrackingMapConfig | None = None,
        *,
        now: Callable[[], float] = time.monotonic,
        concurrency: int = 4,
    ) -> None:
        self._layers: dict[str, BaseLayer] = (
            {layer.id: layer for layer in layers} if source is not None else {}
        )
        self._source = source
        self._cache = cache
        self._config = config or TrackingMapConfig()
        self._limiter = MissLimiter(self._config.misses_per_minute, now)
        self._gate = asyncio.Semaphore(concurrency)

    @property
    def layers(self) -> list[BaseLayer]:
        """The layers offered (street map first); empty without any source."""
        return [self._layers[key] for key in ("map", "aerial") if key in self._layers]

    @property
    def margin_m(self) -> float:
        """Metres around the track where tiles are served."""
        return self._config.margin_m

    async def tile(
        self, account_id: UUID, area: Bounds | None, layer_id: str, z: int, x: int, y: int
    ) -> FetchedTile:
        """Tile ``z/x/y`` of ``layer_id`` when it overlaps ``area`` (the track plus margin)."""
        layer = self._layers.get(layer_id)
        if layer is None or self._source is None:
            msg = "this base map layer is not offered"
            raise TileOutsideMapError(msg)
        if not (tile_in_range(z, x, y) and layer.min_zoom <= z <= layer.max_zoom):
            msg = "the tile is outside the zoom range of the base map"
            raise TileOutsideMapError(msg)
        if area is None or not area.intersects(tile_bounds(z, x, y)):
            msg = "the tile is not around the tracked positions"
            raise TileOutsideMapError(msg)
        url = layer.url(z, x, y)
        key = hashlib.sha256(url.encode()).hexdigest()
        if self._cache is not None:
            cached = await self._cache.get(key, self._config.cache_ttl)
            if cached is not None:
                return cached
        wait = self._limiter.take(account_id)
        if wait is not None:
            msg = "too many new map tiles requested; try again shortly"
            raise AerialRateLimitedError(msg, wait)
        async with self._gate:
            tile = await self._source.fetch(url)
        if self._cache is not None:
            await self._cache.put(key, tile)
        return tile
